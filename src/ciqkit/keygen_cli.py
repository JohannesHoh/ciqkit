"""``ciqkit keygen``: create the developer key the built-in build signs with."""
from __future__ import annotations

import argparse
import json
import sys

from . import build


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="ciqkit keygen",
        description="Create a Connect IQ developer key (RSA 4096, PKCS#8 DER) "
                    "with openssl. Location: $CIQKIT_DEV_KEY, else "
                    "$XDG_CONFIG_HOME/ciqkit/developer_key.der "
                    "(~/.config/ciqkit/developer_key.der). Never overwrites.")
    p.parse_args(argv)
    path = build.dev_key_path()
    try:
        build.generate_dev_key(path)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (build.BuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "key": path}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
