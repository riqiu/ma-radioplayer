"""
Give the provider its own domain: rename its folder, set the domain and name the project.

Run it once, from the root of the repository:

    uv run python scripts/rename.py my_service
"""

import keyword
import re
import sys
from pathlib import Path

REPOSITORY = Path(__file__).parents[1]
# lower case letters, digits and underscores, starting with a letter
DOMAIN = re.compile(r"[a-z][a-z0-9_]*")
# each place that holds the domain, as a template of the text around it
PLACES = {
    "tests/__init__.py": 'DOMAIN = "{}"\n',
    ".github/workflows/ci.yml": "folder: {}\n",
}
# the project name in pyproject.toml, which becomes the domain with dashes
PROJECT_NAME = re.compile(r'^(\[project\]\nname = )".*"$', re.MULTILINE)
# the part of the comment in tests/__init__.py that points to this script
SCRIPT_NOTE = "; scripts/rename.py changes it"


def main() -> int:
    """Rename the provider to the domain given on the command line and return the exit code."""
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    new = sys.argv[1]
    if not DOMAIN.fullmatch(new) or keyword.iskeyword(new):
        print(f"{new!r} is not a domain: lower case letters, digits and underscores")
        return 1
    tests = (REPOSITORY / "tests" / "__init__.py").read_text(encoding="utf-8")
    old = re.search(r'^DOMAIN = "(.+)"$', tests, re.MULTILINE)
    assert old is not None, "tests/__init__.py has no DOMAIN"
    old_domain = old.group(1)
    places = {**PLACES, f"{old_domain}/manifest.json": '"domain": "{}"'}
    changed: dict[Path, str] = {}
    for path, template in places.items():
        file = REPOSITORY / path
        content = file.read_text(encoding="utf-8")
        before, after = template.format(old_domain), template.format(new)
        if content.count(before) != 1:
            print(f"{path} does not hold {before.strip()!r} once; nothing was changed")
            return 1
        changed[file] = content.replace(before, after)
    tests_file = REPOSITORY / "tests" / "__init__.py"
    changed[tests_file] = changed[tests_file].replace(SCRIPT_NOTE, "")
    pyproject = REPOSITORY / "pyproject.toml"
    content, found = PROJECT_NAME.subn(
        rf'\1"{new.replace("_", "-")}"', pyproject.read_text(encoding="utf-8")
    )
    if found != 1:
        print("pyproject.toml does not start [project] with its name; nothing was changed")
        return 1
    changed[pyproject] = content
    for file, content in changed.items():
        file.write_text(content, encoding="utf-8")
    (REPOSITORY / old_domain).rename(REPOSITORY / new)
    print(f"renamed {old_domain} to {new}; now run: uv lock && uv run pytest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
