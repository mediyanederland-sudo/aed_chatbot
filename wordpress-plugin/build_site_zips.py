"""Build five branded WordPress plugins from one source template.

Run from this directory: python build_site_zips.py [--site <slug>] [--export-sources].
Each build checks tenant-specific content anchors and rejects leftover base
store identity, except the shared backend hostname. Edit the template and store
configuration, then use --export-sources to refresh the four store source folders."""

import argparse
import io
import os
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aedleader-chatbot")
DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
VERSION = "1.1.0"
BACKEND_URL = "https://aedleader-chatbot.mediya.dev"  # shared HTTPS front for all tenants

# CPR1-specific content overrides use exact template anchors and lead with training.


_CPR1_CONTENT = {
    # greeting default (class-aedlc-config.php) — PHP source has literal \n\n
    "Hi, I'm Rescue Rita — your AED Leader assistant. I can help you find the right AED, compare products, and answer questions about defibrillators.\\n\\nWhat are you looking for today? And in case we get disconnected, feel free to share your name and email here.":
        "Hi, I'm Rescue Rita — your CPR1 assistant. I can help you book CPR, AED & First Aid training for your team, find the right AED, and answer your questions.\\n\\nAre you interested in training or AED equipment today? And in case we get disconnected, feel free to share your name and email here.",
    # find_aed chip block → course chip FIRST, AED chip second (payload kept —
    # it drives the pinned AED configurator funnel)
    """            [
                'id'      => 'find_aed',
                'label'   => 'Product assistant',
                'action'  => 'prompt',
                'payload' => 'Help me find the right AED for my needs',
            ],""":
        """            [
                'id'      => 'find_course',
                'label'   => 'CPR/First-Aid training',
                'action'  => 'prompt',
                'payload' => 'Help me find the right CPR/First-Aid training course for my team',
            ],
            [
                'id'      => 'find_aed',
                'label'   => 'AED equipment',
                'action'  => 'prompt',
                'payload' => 'Help me find the right AED for my needs',
            ],""",
    "'What questions do most customers ask before buying an AED?'":
        "'What do most customers ask before booking training or buying an AED?'",
    # teaser (config default + widget.php DOM fallback — one anchor hits both)
    "Have questions about AEDs? Ask me here!":
        "Questions about CPR training or AEDs? Ask me here!",
    # input placeholder (templates/widget.php)
    "Ask about AEDs, accessories, or pricing...":
        "Ask about training courses, AEDs, or pricing...",
    # chatbot.js hardcoded fallback greeting sentence (belt-and-braces)
    "I can help you find the right AED for your needs.":
        "I can help with CPR, AED & First Aid training and AED equipment.",
}

# Remove the hours line for MDSI because no published hours are configured.

_MDSI_CONTENT = {
    "\n                        'Hours: Mon to Fri, 8:00 AM to 4:30 PM PST',": "",
}

# Store identity and contact configuration.
SITES = {
    "aedleader": None,  # template ships unmodified
    "responseready": {
        "brand": "Response Ready",
        "site_url": "https://responseready.com",
        "inquiry_url": "https://responseready.com/submit-an-inquiry/",
        "phone": "858-665-2025",
        "email": "info@responseready.com",
        "bot_name": "Preparedness Paige",
    },
    "cpr1": {
        "brand": "CPR1",
        "site_url": "https://cpr1.com",
        "inquiry_url": "https://cpr1.com/contact/",
        "phone": "858-665-8444",
        "email": "info@cpr1.com",
        "bot_name": "Coach Casey",
        "content": _CPR1_CONTENT,
    },
    "mdsimedical": {
        "brand": "MDSI",
        "site_url": "https://mdsimedical.com",
        "inquiry_url": "https://mdsimedical.com/contact-us/",
        "phone": "888-683-7988",
        "email": "customerservice@mdsimedical.com",
        "bot_name": "Rescue Rita",  # awaiting client's pick (absent from every option)
        "content": _MDSI_CONTENT,
    },
    "aedtotalsolution": {
        "brand": "AED Total Solution",
        "site_url": "https://www.aedtotalsolution.com",
        "inquiry_url": "https://www.aedtotalsolution.com/submit-an-inquiry/",
        "phone": "855-263-7772",
        "email": "info@aedtotalsolution.com",
        "bot_name": "Compliance Claire",
    },
}

TEXT_EXTENSIONS = {".php", ".js", ".css", ".md", ".txt", ".html"}


def _transform(text: str, slug: str, facts: dict, relpath: str, used_content=None) -> str:
    """Rebrand one file's content for a sister site (order matters: specific → generic)."""
    # Per-site CONTENT overrides first: anchors are original template text,
    # values are final site wording (identity swaps below can't mangle them).
    for old, new in facts.get("content", {}).items():
        if old in text:
            text = text.replace(old, new)
            if used_content is not None:
                used_content.add(old)
    replacements = [
        # identity / text domain / slugs
        ("aedleader-chatbot", f"{slug}-chatbot"),
        # inquiry chip CTA (before the generic domain replace)
        ("https://www.aedleader.com/submit-an-inquiry/", facts["inquiry_url"]),
        ("https://www.aedleader.com", facts["site_url"]),
        ("https://aedleader.com", facts["site_url"]),  # bare-domain form (README env example)
        ("info@aedleader.com", facts["email"]),
        # bare-domain mentions (CSS/README comments) — after URL/email forms
        ("aedleader.com", facts["site_url"].split("//", 1)[1]),
        ("855-888-2771", facts["phone"]),
        # baked tenant slug (config default + frontend fallback)
        ("'site'          => 'aedleader'", f"'site'          => '{slug}'"),
        ("?? 'aedleader'", f"?? '{slug}'"),
        # display branding (after slug-specific replaces)
        ("AED Leader Chatbot", f"{facts['brand']} Chatbot"),
        # Replace the generic WordPress admin menu label as well as the full brand name.

        ("AED Chatbot", f"{facts['brand']} Chatbot"),
        ("AED Leader", facts["brand"]),
        # camel-case @package annotations (AEDLeader_Chatbot)
        ("AEDLeader", facts["brand"].replace(" ", "")),
        # Apply the persona name after the content overrides.

        ("Rescue Rita", facts.get("bot_name", "Rescue Rita")),
    ]
    for old, new in replacements:
        text = text.replace(old, new)

    # Bake the shared backend URL into sister-store defaults for activation.

    if relpath.endswith("class-aedlc-config.php"):
        anchor = "'backend_url'   => '',"
        assert anchor in text, "backend_url default anchor missing"
        text = text.replace(anchor, f"'backend_url'   => '{BACKEND_URL}',")

    return text


def _verify_clean(text: str, relpath: str, slug: str, facts: dict = None) -> None:
    """No aedleader identity may survive in a sister build (backend domain exempt)."""
    probe = text.replace("aedleader-chatbot.mediya.dev", "")
    for needle in ("aedleader", "AED Leader", "855-888-2771"):
        assert needle.lower() not in probe.lower(), (
            f"{slug}/{relpath}: {needle!r} survived rebranding")
    if facts and facts.get("bot_name", "Rescue Rita") != "Rescue Rita":
        assert "Rescue Rita" not in text, (
            f"{slug}/{relpath}: 'Rescue Rita' survived the persona rename")


def build_zip(slug: str) -> str:
    facts = SITES[slug]
    os.makedirs(DIST_DIR, exist_ok=True)
    out_path = os.path.join(DIST_DIR, f"{slug}-chatbot-{VERSION}.zip")
    top = f"{slug}-chatbot"

    used_content: set = set()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(TEMPLATE_DIR):
            for fn in files:
                abs_path = os.path.join(root, fn)
                rel = os.path.relpath(abs_path, TEMPLATE_DIR).replace(os.sep, "/")
                arc_rel = rel
                if facts is not None and rel == "aedleader-chatbot.php":
                    arc_rel = f"{slug}-chatbot.php"  # main file matches folder
                arcname = f"{top}/{arc_rel}"

                ext = os.path.splitext(fn)[1].lower()
                if facts is not None and ext in TEXT_EXTENSIONS:
                    text = io.open(abs_path, encoding="utf-8").read()
                    text = _transform(text, slug, facts, rel, used_content)
                    _verify_clean(text, rel, slug, facts)
                    zf.writestr(arcname, text)
                else:
                    zf.write(abs_path, arcname)

    # Template drift guard: every content-override anchor must have matched
    # somewhere in the build, else the site silently ships old wording.
    if facts is not None:
        unused = set(facts.get("content", {})) - used_content
        assert not unused, (
            f"{slug}: content-override anchors not found in template "
            f"(template drifted?): {sorted(a[:60] for a in unused)}")

    return out_path


UNPACKED_DIR = os.path.join(DIST_DIR, "unpacked")

UNPACKED_README = """BUILD ARTIFACT — DO NOT EDIT ANY FILE IN THIS TREE.

These folders are extracted from the per-site zips for FTP/FileZilla partial
updates (upload only the changed file into the live site's
wp-content/plugins/<slug>-chatbot/).

Source of truth is the TEMPLATE: wordpress-plugin/aedleader-chatbot/
(yes, also for aedleader itself — the folder next to this one is a copy).
Edit the template, then rerun:  python build_site_zips.py
Every build wipes and regenerates this whole tree, so local edits here are
LOST and were never what the zips shipped anyway.
"""


def extract_zip(zip_path: str) -> None:
    """Extract a generated plugin into dist/unpacked for partial uploads.
    Replace the previous generated copy so it matches the ZIP."""
    with zipfile.ZipFile(zip_path) as zf:
        top = zf.namelist()[0].split("/")[0]
        target = os.path.join(UNPACKED_DIR, top)
        if os.path.isdir(target):
            shutil.rmtree(target)
        zf.extractall(UNPACKED_DIR)


def export_sources(zip_path: str, slug: str) -> None:
    """Refresh a store source folder without overwriting the shared template."""
    if slug not in SITES:
        raise ValueError(f"Unknown site: {slug}")
    if slug == "aedleader":
        return
    parent = Path(TEMPLATE_DIR).resolve().parent
    target = parent / f"{slug}-chatbot"
    if target.resolve().parent != parent:
        raise ValueError(f"Source folder must remain inside {parent}")
    outputs = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            parts = PurePosixPath(name).parts
            if (len(parts) < 2 or parts[0] != target.name
                    or any(part in {".", ".."} for part in parts)):
                raise ValueError(f"Unexpected archive member: {name}")
            path = target.joinpath(*parts[1:])
            if not path.resolve().is_relative_to(target.resolve()):
                raise ValueError(f"Archive member leaves source folder: {name}")
            outputs[path] = archive.read(name)
    extra = {path for path in target.rglob("*") if path.is_file()} - outputs.keys()
    if extra:
        raise ValueError(f"Unexpected files in {target}; review before exporting: {sorted(extra)}")
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default=None, help="build one site only (slug)")
    parser.add_argument("--export-sources", action="store_true",
                        help="refresh the four branded source folders beside the shared template")
    args = parser.parse_args()

    slugs = [args.site] if args.site else list(SITES)
    for slug in slugs:
        if slug not in SITES:
            sys.exit(f"Unknown site '{slug}'. Valid: {', '.join(SITES)}")
        path = build_zip(slug)
        extract_zip(path)
        if args.export_sources:
            export_sources(path, slug)
        size = os.path.getsize(path) // 1024
        print(f"built {os.path.basename(path)}  ({size} KB)  [+ unpacked/]")

    os.makedirs(UNPACKED_DIR, exist_ok=True)
    with io.open(os.path.join(UNPACKED_DIR, "_README_DO_NOT_EDIT.txt"),
                 "w", encoding="utf-8") as f:
        f.write(UNPACKED_README)


if __name__ == "__main__":
    main()
