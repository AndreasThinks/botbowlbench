"""Rebuild botbowl/web/static/dist/js/botbowl.js (what gulp's 'scripts' task does) without node."""
import os

ORDER = ["app.js", "controllers.js", "directives.js", "filters.js", "services.js"]


def main():
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "botbowl", "web", "static")
    parts = []
    for name in ORDER:
        with open(os.path.join(base, "js", name), encoding="utf-8") as f:
            parts.append(f.read())
    with open(os.path.join(base, "dist", "js", "botbowl.js"), "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


if __name__ == "__main__":
    main()
