# Operating Photobook Radar on this Mac

Open the **Photobook Radar** shortcut on the Desktop, or visit `http://127.0.0.1:8765` on this Mac. Finds and Urgent will contain fresh discoveries only; the old imported listings remain under Archive as reference material. A promising label is not a current stock check or a valuation; open the seller link before acting.

The installation uses three macOS LaunchAgents: `com.jonattenborough.photobook-radar.web`, `.worker`, and `.backup`. The service is designed to recover after a process crash or a login session restart. FileVault requires the Mac's storage to be unlocked; a per-user LaunchAgent does not run at the pre-login screen. Reboot, sleep, screen lock and logout recovery still need an observed test. Keep this Mac powered, online and signed into Jon's user session for routine operation.

From Terminal in `/Users/jon/Documents/Book Radar/oxfam-photobook-monitor`:

```sh
./scripts/service status
./scripts/doctor
./scripts/service restart
./scripts/backup
./scripts/restore-test "$(ls -t "$HOME/Library/Application Support/Photobook Radar/backups"/*.db.gz | head -1)"
```

The private application data and backup directory are under `~/Library/Application Support/Photobook Radar/`; logs are in its `logs` folder. The restore test makes a temporary isolated copy and removes it after checking integrity. The dashboard passphrase is in the owner-only `~/Library/Application Support/Photobook Radar/dashboard-passphrase.txt` file on this Mac. It can be changed with `.venv/bin/python -m photobook_radar.cli set-passphrase`. Credentials and the Telegram chat ID are already stored privately. Telegram has accepted one labelled test message; Jon confirmed its arrival on his phone.

The current installation is a **shadow archive with a fresh monitoring epoch**. Historical screening jobs were cancelled; older records are no longer actionable Finds. The [search menu](search-menu-for-jon.md) lists every proposed lane, its cadence and scale. Built local source switches default to off while Jon chooses. The installation does not yet replace GitHub scanning or send automatic alerts. Source cutover, remote phone access and AI research have not been enabled. Jon selected Codex allowance for later deeper assessments; the planned default is GPT-6 Sol for selected leads, with a measured sample before routine use.
