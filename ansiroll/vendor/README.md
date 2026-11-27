# Bundled template runtime

`jinja2-py35.zip` contains the pure-Python sources for:

- Jinja2 2.11.3
- MarkupSafe 1.1.1

These are the final releases of this dependency pair that support Python 3.5.
The archive also contains both upstream license files. Its lexer backports
Jinja 3.1's `+` closing modifiers for comments, statement blocks, and
`endraw` so those tags can preserve the following newline when
`trim_blocks` is enabled.

Upstream artifacts used to build it:

- `Jinja2-2.11.3-py2.py3-none-any.whl` (SHA-256
  `03e47ad063331dd6a3f04a43eddca8a966a26ba0c5b7207a9a9e4e08f1b29419`)
- `MarkupSafe-1.1.1.tar.gz` (SHA-256
  `29872e92839765e546828bb7754a68c418d927cd064fd4708fab9fe9c8bb116b`)

The generated archive's SHA-256 is
`d2367220e0d89d685c8685c88f57499568c2fc8652d367cdb83e28430674efc0`.

`pyyaml-py35.zip` contains the pure-Python sources from PyYAML 5.3.1,
stored under the private import name `ansiroll_yaml`. PyYAML 5.3.1 is the last
release line supporting Python 3.5. The runtime exposes only its safe loader and
dumper APIs for Ansible's YAML filters. The archive includes the upstream MIT
license.

Upstream artifact used to build it:

- `PyYAML-5.3.1.tar.gz` (SHA-256
  `b8eac752c5e14d3eca0e6dd9199cd627518cb5ec06add0de9d32baeee6fe645d`)

Rebuild it with `python3 tools/build_vendor_yaml.py`. The generated archive's
SHA-256 is
`bda4e7b81d96fdce9999770570c34fbc07209d5182c1d6cc468d3236898fcc82`.
