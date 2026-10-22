#!/usr/bin/env python3
"""
Build the bilingual (EN/DE) mailcow docs with Zensical.

Tested with Zensical 0.0.71 (October 2026) against mailcow-dockerized-docs.

Zensical reads mailcow's mkdocs.yml natively and handles the theme, Markdown
extensions, search, redirects, social cards and glightbox itself. Three plugins
have no Zensical equivalent yet, so this script does their job:

  i18n (mkdocs-static-i18n)
      Splits docs/ (page.en.md / page.de.md) into one tree per language, falling
      back to English where a translation is missing, rewrites links that point
      at page.en.md, builds each tree and assembles English at / and German at
      /de/ with the header language selector. German nav titles and site name
      come from the i18n plugin settings in mkdocs.yml.
  git-revision-date-localized
      Writes each page's last commit date into its front matter, which the theme
      shows at the bottom of the page. Needs the full git history (mailcow's CI
      already checks out with fetch-depth: 0).
  open-in-new-tab
      Adds a small script that opens external links in a new tab, plus the
      external-link icon when add_icon is set.

Every other setting is read from mkdocs.yml at build time, so changes there
are picked up automatically.

Usage, from the repository root (Python 3.11+, PyYAML comes with Zensical):
    pip install zensical
    python build_i18n_site.py        # -> ./site (English) and ./site/de (German)
"""
import copy
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
OUT = ROOT / "site"
WORK = ROOT / ".cache" / "zensical-i18n"             # per-language projects (.cache is gitignored)
SOCIAL_CACHE = ROOT / ".cache" / "plugin" / "social"  # font and card cache shared by both builds

# Plugins this script replaces; they are removed from the generated configs.
HANDLED = {"i18n", "git-revision-date-localized", "open-in-new-tab"}


# --- mkdocs.yml in and out, keeping !!python/name tags ----------------------

class PyName(str):
    """A !!python/name: value from mkdocs.yml, kept so it can be written back."""


class Loader(yaml.SafeLoader):
    pass


class Dumper(yaml.SafeDumper):
    pass


def _load_pyname(loader, suffix, node):
    # Zensical ships its own emoji index and generator.
    return PyName(suffix.replace("material.extensions.emoji.", "zensical.extensions.emoji."))


Loader.add_multi_constructor("tag:yaml.org,2002:python/name:", _load_pyname)
Dumper.add_representer(
    PyName, lambda dumper, value: dumper.represent_scalar(f"tag:yaml.org,2002:python/name:{value}", "")
)

MK = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=Loader)


def _plugins(value):
    for item in value or []:
        if isinstance(item, str):
            yield item, {}
        else:
            (name, conf), = item.items()
            yield name, conf or {}


PLUGINS = list(_plugins(MK.get("plugins")))
CONF = dict(PLUGINS)
I18N = CONF.get("i18n") or sys.exit("mkdocs.yml has no i18n plugin configuration")
LANGS = [lang for lang in I18N["languages"] if lang.get("build", True)]
DEFAULT = next((lang["locale"] for lang in LANGS if lang.get("default")), LANGS[0]["locale"])
FALLBACK = I18N.get("fallback_to_default", True)

_LOCALES = "|".join(re.escape(lang["locale"]) for lang in LANGS)
SUFFIXED = re.compile(rf"^(?P<stem>.+)\.(?P<lang>{_LOCALES})\.(?P<ext>[^.]+)$")
PAGE_LINK = re.compile(
    rf"(?P<pre>\]\(\s*<?|\]:\s*<?|href=[\"'])(?P<path>[^)\s\"'<>#]+?)"
    rf"\.(?:{_LOCALES})\.md/?(?P<anchor>#[^)\s\"'<>]*)?(?=[)\s\"'>]|$)",
    re.M,
)

_BASE = urlparse(MK.get("site_url") or "/").path or "/"
BASE = _BASE if _BASE.endswith("/") else _BASE + "/"

NEWTAB_JS = "assets/javascripts/open-in-new-tab.js"
NEWTAB_CSS = "assets/stylesheets/open-in-new-tab.css"
NEWTAB_JS_SOURCE = """\
/* Opens external links in a new tab (replacement for mkdocs-open-in-new-tab). */
(function () {
  function update() {
    document.querySelectorAll(".md-content a[href]").forEach(function (a) {
      if (!/^https?:$/.test(a.protocol) || a.hostname === location.hostname) return;
      if (a.classList.contains("glightbox")) return;
      a.target = "_blank";
      a.rel = "noopener";
      if (!a.querySelector("img")) a.classList.add("external-link");
    });
  }
  if (typeof document$ !== "undefined") document$.subscribe(update);
  else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", update);
  else update();
})();
"""
_ICON = (
    "M14,3V5H17.59L7.76,14.83L9.17,16.24L19,6.41V10H21V3M19,19H5V5H12V3H5"
    "C3.89,3 3,3.9 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V12H19V19Z"
)
NEWTAB_CSS_SOURCE = f"""\
/* External-link icon (replacement for mkdocs-open-in-new-tab add_icon). */
.md-typeset a.external-link::after {{
  content: "";
  display: inline-block;
  width: 0.85em;
  height: 0.85em;
  margin-left: 0.15em;
  vertical-align: -0.1em;
  background-color: currentColor;
  -webkit-mask: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{_ICON}"/></svg>') center / contain no-repeat;
  mask: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{_ICON}"/></svg>') center / contain no-repeat;
}}
"""


def strip_suffix(path: str) -> str:
    """page.en.md -> page.md (redirect targets were written for the suffix layout)."""
    return re.sub(rf"\.(?:{_LOCALES})\.md$", ".md", path)


def _fix_link(match):
    if match["path"].startswith(("http://", "https://", "//")):
        return match[0]
    return f"{match['pre']}{match['path']}.md{match['anchor'] or ''}"


# --- git-revision-date-localized --------------------------------------------

def git_dates() -> dict:
    """Last commit (author) time of every file under docs/, from one git log pass."""
    git = ["git", "-C", str(ROOT)]
    try:
        shallow = subprocess.run(git + ["rev-parse", "--is-shallow-repository"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        log = subprocess.run(git + ["-c", "core.quotepath=off", "log", "--format=%x00%at",
                                    "--name-only", "--", "docs"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        print("warning: no git history available, pages get no revision date")
        return {}
    if shallow == "true":
        print("warning: shallow clone, page dates will be wrong (check out with fetch-depth: 0)")
    dates, stamp = {}, None
    for line in log.splitlines():
        if line.startswith("\x00"):
            stamp = int(line[1:])
        elif line and stamp is not None:
            dates.setdefault(line, stamp)       # newest commit comes first
    return dates


def date_formatter():
    conf = CONF.get("git-revision-date-localized")
    if conf is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(conf.get("timezone", "UTC"))
    except Exception:
        print("warning: time zone data not available, page dates are shown in UTC")
        zone = timezone.utc
    pattern = "%Y-%m-%d %H:%M:%S" if conf.get("type") == "iso_datetime" else "%Y-%m-%d"
    return lambda stamp: datetime.fromtimestamp(stamp, zone).strftime(pattern)


def add_front_matter(text: str, key: str, value: str) -> str:
    line = f'{key}: "{value}"\n'
    if text.startswith(("---\n", "---\r\n")):
        cut = text.index("\n") + 1
        return text[:cut] + line + text[cut:]
    return f"---\n{line}---\n\n{text}"


# --- per-language docs tree ---------------------------------------------------

def build_tree(locale: str, dst: Path, dates: dict, fmt) -> int:
    pages, variants = {}, {}
    for src in sorted(DOCS.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(DOCS)
        match = SUFFIXED.match(src.name)
        if match and match["ext"] == "md":
            pages.setdefault(rel.with_name(f"{match['stem']}.md"), {})[match["lang"]] = src
            continue
        # Assets are copied under their own name, so explicit references such as
        # image.en.png keep working in every language ...
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        if match:
            variants.setdefault(rel.with_name(f"{match['stem']}.{match['ext']}"), {})[match["lang"]] = src
    # ... and image.<lang>.png is also published as image.png for this language.
    for rel, by_lang in variants.items():
        src = by_lang.get(locale) or (by_lang.get(DEFAULT) if FALLBACK else None)
        if src and not (dst / rel).exists():
            shutil.copy2(src, dst / rel)
    for rel, by_lang in pages.items():
        src = by_lang.get(locale) or (by_lang.get(DEFAULT) if FALLBACK else None)
        if src is None:
            continue
        text = PAGE_LINK.sub(_fix_link, src.read_text(encoding="utf-8"))
        stamp = dates.get(src.relative_to(ROOT).as_posix())
        if fmt and stamp:
            text = add_front_matter(text, "git_revision_date_localized", fmt(stamp))
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return len(pages)


# --- per-language config ------------------------------------------------------

def translate_nav(items, table):
    out = []
    for item in items:
        if isinstance(item, dict):
            (title, value), = item.items()
            value = translate_nav(value, table) if isinstance(value, list) else value
            out.append({table.get(title, title): value})
        else:
            out.append(item)
    return out


def config_for(lang: dict) -> dict:
    locale = lang["locale"]
    is_default = locale == DEFAULT
    conf = copy.deepcopy(MK)
    conf.pop("remote_branch", None)                  # only used by mkdocs gh-deploy
    if lang.get("site_name"):
        conf["site_name"] = lang["site_name"]
    if not is_default and conf.get("site_url"):
        conf["site_url"] = conf["site_url"].rstrip("/") + f"/{locale}/"
    conf.setdefault("theme", {})["language"] = locale
    if lang.get("nav_translations") and conf.get("nav"):
        conf["nav"] = translate_nav(conf["nav"], lang["nav_translations"])
    conf.setdefault("extra", {})["alternate"] = [
        {"name": other.get("name", other["locale"]),
         "link": BASE if other["locale"] == DEFAULT else f"{BASE}{other['locale']}/",
         "lang": other["locale"]}
        for other in LANGS
    ]
    plugins = []
    for name, pconf in PLUGINS:
        if name in HANDLED:
            continue
        pconf = copy.deepcopy(pconf)
        if name == "redirects":
            if not is_default:
                continue                               # redirect stubs live at the site root
            pconf["redirect_maps"] = {strip_suffix(k): strip_suffix(v)
                                      for k, v in (pconf.get("redirect_maps") or {}).items()}
        elif name == "social":
            pconf["cache_dir"] = str(SOCIAL_CACHE)
        plugins.append({name: pconf} if pconf else name)
    conf["plugins"] = plugins
    newtab = CONF.get("open-in-new-tab")
    if newtab is not None:
        conf["extra_javascript"] = list(conf.get("extra_javascript") or []) + [NEWTAB_JS]
        if newtab.get("add_icon"):
            conf["extra_css"] = list(conf.get("extra_css") or []) + [NEWTAB_CSS]
    return conf


def zensical_command() -> list:
    exe = shutil.which("zensical")
    return [exe] if exe else [sys.executable, "-m", "zensical"]


def main():
    for path in (WORK, OUT):
        if path.exists():
            shutil.rmtree(path)
    dates = git_dates() if "git-revision-date-localized" in CONF else {}
    fmt = date_formatter()
    copy_dirs = {d for d in (MK.get("theme", {}).get("custom_dir"), "includes") if d and (ROOT / d).is_dir()}
    for lang in LANGS:
        locale = lang["locale"]
        project = WORK / locale
        count = build_tree(locale, project / "docs", dates, fmt)
        for name in copy_dirs:
            shutil.copytree(ROOT / name, project / name)
        if "open-in-new-tab" in CONF:
            (project / "docs" / NEWTAB_JS).parent.mkdir(parents=True, exist_ok=True)
            (project / "docs" / NEWTAB_JS).write_text(NEWTAB_JS_SOURCE, encoding="utf-8")
            (project / "docs" / NEWTAB_CSS).parent.mkdir(parents=True, exist_ok=True)
            (project / "docs" / NEWTAB_CSS).write_text(NEWTAB_CSS_SOURCE, encoding="utf-8")
        (project / "mkdocs.yml").write_text(
            yaml.dump(config_for(lang), Dumper=Dumper, allow_unicode=True, sort_keys=False, width=4096),
            encoding="utf-8",
        )
        print(f"[{locale}] {count} pages, building ...", flush=True)
        if subprocess.run(zensical_command() + ["build"], cwd=project).returncode:
            sys.exit(f"zensical build failed for '{locale}'")
    shutil.copytree(WORK / DEFAULT / "site", OUT)
    for lang in LANGS:
        if lang["locale"] != DEFAULT:
            shutil.copytree(WORK / lang["locale"] / "site", OUT / lang["locale"])
    print(f"\nDone -> {OUT}  (" + ", ".join(
        f"{lang['locale']}: {BASE if lang['locale'] == DEFAULT else BASE + lang['locale'] + '/'}"
        for lang in LANGS) + ")")


if __name__ == "__main__":
    main()
