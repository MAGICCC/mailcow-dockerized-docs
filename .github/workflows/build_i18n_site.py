#!/usr/bin/env python3
"""
Build the bilingual (EN/DE) mailcow docs with Zensical, WITHOUT mkdocs-static-i18n.

Zensical (as of 0.0.58) has no content-i18n plugin, but it does support a header
LANGUAGE SELECTOR via [project.extra] alternate. This script reproduces the
mkdocs-static-i18n "suffix" behaviour manually:

  1. Split the docs/ tree (foo.en.md / foo.de.md) into two per-language trees,
     stripping the language suffix (with fallback to the default language).
  2. Generate a zensical.toml per language (English at /, German at /de/), each
     wired with the alternate language selector. German nav titles are translated
     using the nav_translations already present in mkdocs.yml.
  3. Build both and assemble a combined site: English at the root, German at /de/.

Run from the repository root (where mkdocs.yml lives):
    python build_i18n_site.py
Output: ./site  (open ./site/index.html and ./site/de/index.html)

Requires: pyyaml, zensical  (pip install pyyaml zensical)
"""
import re, shutil, subprocess, sys
from pathlib import Path

ROOT    = Path(__file__).resolve().parent
DOCS    = ROOT / "docs"
WORK    = ROOT / ".zensical-i18n"      # scratch dir for the two per-language projects
OUT     = ROOT / "site"                # final combined output
LANGS   = ["en", "de"]
DEFAULT = "en"
LANG_RE = re.compile(r"^(.*)\.(en|de)\.([^.]+)$")

import yaml
class _L(yaml.SafeLoader): pass
_L.add_multi_constructor("tag:yaml.org,2002:python/name:", lambda *a: None)
mk = yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_L)

nav, navtr, redirect_maps = mk["nav"], {}, {}
for p in mk.get("plugins", []):
    if isinstance(p, dict) and "i18n" in p:
        for lc in p["i18n"]["languages"]:
            if lc.get("locale") == "de":
                navtr = lc.get("nav_translations", {})
    if isinstance(p, dict) and "redirects" in p:
        redirect_maps = p["redirects"].get("redirect_maps", {})


def build_tree(lang: str, dst: Path):
    """Materialise a single-language docs tree (suffix i18n + fallback_to_default)."""
    bases, plains = {}, []
    for f in DOCS.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(DOCS)
        m = LANG_RE.match(f.name)
        if m:
            base = rel.with_name(f"{m.group(1)}.{m.group(3)}")
            bases.setdefault(str(base), {})[m.group(2)] = f
        else:
            plains.append(rel)
    for rel in plains:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DOCS / rel, dst / rel)
    for base, by_lang in bases.items():
        chosen = by_lang.get(lang) or by_lang.get(DEFAULT)
        if chosen:
            (dst / base).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(chosen, dst / base)


def _esc(s): return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
def _tr(t, de): return navtr.get(t, t) if de else t
def _emit(item, ind, de):
    pad = "  " * ind
    if isinstance(item, str):
        return pad + _esc(item)
    (title, val), = item.items()
    if isinstance(val, str):
        return pad + "{ " + _esc(_tr(title, de)) + " = " + _esc(val) + " }"
    inner = ",\n".join(_emit(c, ind + 1, de) for c in val)
    return pad + "{ " + _esc(_tr(title, de)) + " = [\n" + inner + "\n" + pad + "] }"
def nav_toml(de): return "nav = [\n" + ",\n".join(_emit(t, 1, de) for t in nav) + "\n]\n"

PALETTE = """[[project.theme.palette]]
media = "(prefers-color-scheme)"
toggle.icon = "material/brightness-auto"
toggle.name = "Switch to light mode"
[[project.theme.palette]]
media = "(prefers-color-scheme: light)"
scheme = "default"
toggle.icon = "material/brightness-7"
toggle.name = "Switch to dark mode"
[[project.theme.palette]]
media = "(prefers-color-scheme: dark)"
scheme = "slate"
toggle.icon = "material/brightness-4"
toggle.name = "Switch to system preference"
"""
ALTERNATE = """[project.extra]
alternate = [
  { name = "English", link = "/", lang = "en" },
  { name = "Deutsch", link = "/de/", lang = "de" },
]
"""
SOCIAL = """[[project.extra.social]]
icon = "fontawesome/solid/globe"
link = "https://mailcow.email"
[[project.extra.social]]
icon = "fontawesome/brands/github-alt"
link = "https://github.com/mailcow"
[[project.extra.social]]
icon = "fontawesome/brands/x-twitter"
link = "https://x.com/mailcow_email"
[[project.extra.social]]
icon = "fontawesome/brands/mastodon"
link = "https://mailcow.social/@doncow"
"""
EXT = """[project.markdown_extensions.abbr]
[project.markdown_extensions.attr_list]
[project.markdown_extensions.admonition]
[project.markdown_extensions.md_in_html]
[project.markdown_extensions.tables]
[project.markdown_extensions.footnotes]
[project.markdown_extensions.toc]
permalink = true
[project.markdown_extensions.pymdownx.magiclink]
[project.markdown_extensions.pymdownx.tasklist]
custom_checkbox = true
[project.markdown_extensions.pymdownx.mark]
[project.markdown_extensions.pymdownx.caret]
[project.markdown_extensions.pymdownx.details]
[project.markdown_extensions.pymdownx.tilde]
[project.markdown_extensions.pymdownx.betterem]
[project.markdown_extensions.pymdownx.snippets]
auto_append = ["includes/abbreviations.md"]
[project.markdown_extensions.pymdownx.superfences]
[project.markdown_extensions.pymdownx.tabbed]
alternate_style = true
[project.markdown_extensions.pymdownx.emoji]
emoji_index = "zensical.extensions.emoji.twemoji"
emoji_generator = "zensical.extensions.emoji.to_svg"
"""

def _strip_lang(path: str) -> str:
    # redirect targets in mkdocs.yml were written for the suffix layout
    # (e.g. "...integration.en.md"); the split tree has no suffix, so normalise.
    return re.sub(r"\.(en|de)\.md$", ".md", path)

def redirects_toml():
    if not redirect_maps:
        return ""
    lines = ["[project.plugins.redirects.redirect_maps]"]
    for k, v in redirect_maps.items():
        lines.append(f"{_esc(_strip_lang(k))} = {_esc(_strip_lang(v))}")
    return "\n".join(lines) + "\n"

def toml_for(lang: str) -> str:
    de = lang == "de"
    site_name = "mailcow: dockerized Dokumentation" if de else "mailcow: dockerized documentation"
    site_url  = "https://docs.mailcow.email/de/" if de else "https://docs.mailcow.email/"
    head = f'''[project]
site_name = "{site_name}"
site_url = "{site_url}"
copyright = "Copyright &copy; <script>document.write(new Date().getFullYear())</script> mailcow Team & Community"
repo_name = "mailcow/mailcow-dockerized"
repo_url = "https://github.com/mailcow/mailcow-dockerized"
edit_uri = "../mailcow-dockerized-docs/edit/master/docs/"
extra_css = ["assets/stylesheets/extra.css"]
extra_javascript = ["assets/javascripts/client.js"]
'''
    theme = f'''[project.theme]
variant = "classic"
custom_dir = "overrides"
language = "{lang}"
logo = "assets/images/logo.svg"
favicon = "assets/images/favicon.png"
features = ["navigation.top","navigation.tracking","announce.dismiss","content.tabs.link","content.tooltips","content.code.copy","search.share","search.highlight"]
'''
    # redirects only in the default-language (root) build
    redir = redirects_toml() if lang == DEFAULT else ""
    return head + nav_toml(de) + "\n" + theme + PALETTE + "\n" + ALTERNATE + "\n" + SOCIAL + "\n" + EXT + ("\n" + redir if redir else "")


def main():
    if WORK.exists(): shutil.rmtree(WORK)
    if OUT.exists():  shutil.rmtree(OUT)
    for lang in LANGS:
        proj = WORK / lang
        build_tree(lang, proj / "docs")
        shutil.copytree(ROOT / "overrides", proj / "overrides")
        shutil.copytree(ROOT / "includes",  proj / "includes")
        (proj / "zensical.toml").write_text(toml_for(lang))
        print(f"[{lang}] wrote project ({len(list((proj/'docs').rglob('*.md')))} pages), building...")
        r = subprocess.run(["zensical", "build"], cwd=proj)
        if r.returncode != 0:
            sys.exit(f"zensical build failed for {lang}")
    # Assemble: default language at root, others under /<lang>/
    shutil.copytree(WORK / DEFAULT / "site", OUT)
    for lang in LANGS:
        if lang != DEFAULT:
            shutil.copytree(WORK / lang / "site", OUT / lang)
    print(f"\nDone -> {OUT}\n  root: {OUT/'index.html'}\n  de:   {OUT/'de'/'index.html'}")

if __name__ == "__main__":
    main()
