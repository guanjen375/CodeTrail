#!/usr/bin/env python3
"""Import local compilation evidence; never run a compiler or a build."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_context import BuildContextError, import_build_context, load_build_context
from root_safety import validate_aicode_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--target", help="Explicit profile label; never inferred from object paths")
    parser.add_argument("--variant")
    parser.add_argument("--compile-commands")
    parser.add_argument("--build-log")
    parser.add_argument("--entry-output", action="append", default=[], help="Select exact output field; repeat for multiple TUs")
    parser.add_argument("--builtin-macros", help="Existing complete preprocessor macro dump for this exact compiler/flags")
    parser.add_argument("--generated-header", action="append", default=[], help="Explicit additional header, path and content bound")
    parser.add_argument("--show", action="store_true", help="Inspect target/unknown evidence without writing")
    args = parser.parse_args(argv)
    try:
        root, error = validate_aicode_root(args.root, str(Path.home()), False)
        if error:
            raise BuildContextError(error)
        if args.show:
            result = load_build_context(root, args.target).summary()
        else:
            if not args.target:
                parser.error("--target is required for import")
            result = import_build_context(root, args.target, compile_commands=args.compile_commands,
                build_log=args.build_log, variant=args.variant, entry_outputs=args.entry_output,
                builtin_macros=args.builtin_macros, generated_headers=args.generated_header)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BuildContextError, OSError, ValueError) as exc:
        print(f"build context: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
