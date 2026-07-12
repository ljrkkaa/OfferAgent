# OfferAgent for Obsidian

OfferAgent connects an Obsidian vault to the local OfferAgent server. It supports vault sync and search, streamed chat with conversation history, Agent selection, cited answers, and review-before-apply VaultActions.

## Development

From `src/interface/obsidian`:

```bash
yarn install
yarn test
yarn build
```

Install `main.js`, `manifest.json`, and `styles.css` under:

```text
<vault>/.obsidian/plugins/offeragent/
```

The default server is `http://127.0.0.1:42110`. Anonymous access is intended for a loopback server, including a forwarded localhost port. For LAN access, start the server with explicit credentials and a bootstrap token, then enter the same token in the plugin's API key setting:

```bash
KHOJ_HOST=0.0.0.0 \
KHOJ_ADMIN_EMAIL=you@example.com \
KHOJ_ADMIN_PASSWORD='a-unique-password' \
KHOJ_API_KEY='kk-your-url-safe-secret' \
bash scripts/run_local.sh
```
