# Operating Photobook Radar on this Mac

Open **Photobook Radar** from the Desktop shortcut, or visit `http://127.0.0.1:8765` on the Mac. The System page shows the 12 selected search lanes and their last success or error. Finds and Urgent show current candidates; older imported listings stay in Archive. Open the seller link to confirm stock, condition and edition before buying.

The Mac Studio is the scanner and Telegram is the alert channel. GitHub keeps the source code and archived history; its seven old scheduled scanners were removed from the remote default branch on 28 September 2026. Manual GitHub workflow triggers remain available. A first pass through each route quietly records existing stock. GPT‑6 Sol researches promising books. Telegram only receives fixed-price gems with checked comparable prices and a strong estimated margin; other interesting books stay in the dashboard. GPT‑6 Luna runs the broader web, publisher and prize sweeps. There is no local daily AI job cap. Codex account limits can still pause research; queued jobs retry after a temporary backoff. Default bargain settings are £150 maximum all-in, at least 60% below the lowest checked comparable and at least £50 indicative net resale room after costs. Change these under `[policy]` in the private `config.toml` and restart the worker.

To investigate a book further, **reply to its Radar alert in Telegram** with a photo or screenshot. You can send the cover, copyright page, signature, condition details, or an eBay/Oxfam product screenshot. The bot matches your reply to that alert and returns a short image-based assessment, usually within a minute when research capacity is available. You may include a short caption such as “Is this the first printing?” Send several images as separate replies. A standalone photo without replying to an alert cannot be matched reliably and will not be researched. Telegram images are downloaded into a private temporary folder on this Mac for analysis and deleted afterward. The model counts only what the image shows; confirm current stock and seller claims before buying.

From Terminal in `/Users/jon/Documents/Book Radar/oxfam-photobook-monitor`:

```sh
./scripts/service status
./scripts/doctor
./scripts/service restart
./scripts/backup
```

The owner-only settings, credentials, database, backups and logs are under `~/Library/Application Support/Photobook Radar/`. Do not put the Telegram or eBay secrets in the repository. The dashboard passphrase is stored locally in the owner-only `dashboard-passphrase.txt` file and can be changed with `.venv/bin/python -m photobook_radar.cli set-passphrase`.

To stop one search, edit its setting under `[sources]` in the local `config.toml` to `false`, then run `./scripts/service restart`. The [search menu](search-menu-for-jon.md) explains each switch. To pause every scan and phone alert, restore `config.shadow-20260928.toml` as `config.toml` and restart. The Mac needs power, internet and Jon’s logged-in user session; FileVault prevents per-user agents from running before unlock. Reboot, logout and 24-hour recovery checks are still to be observed.
