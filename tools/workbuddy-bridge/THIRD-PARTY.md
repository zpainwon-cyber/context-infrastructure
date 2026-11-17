# Third-party components

## vendor/dsh-connect-workbuddy-5.24.8.tgz

Unmodified distribution of
[dingminhua/dsh-connect-workbuddy](https://github.com/dingminhua/dsh-connect-workbuddy),
**MIT License**, 1696 stars as of 2026-11-16.

It is vendored for one reason: `wbctl.py init` on a fresh machine must be able
to install the runtime with no network and no DeepSeek Harness install. The
package is never executed at install time; `npm install` reads it and lays down
`node_modules/dsh-connect-workbuddy`, which `shim.mjs` then imports.

Verify the vendored bytes are the upstream bytes:

```bash
cd vendor && shasum -a 256 -c SHA256SUMS.txt
```

Nothing else in this directory is third-party.
