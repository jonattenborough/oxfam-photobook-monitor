# Cutover and rollback

**Cutover recorded 28 September 2026.** PR #3533 removed the schedules from seven GitHub workflows on `main`; a check of the remote workflows found no remaining `schedule:` triggers. The Mac Studio was then switched to `mode = "production"` with all 12 source switches selected, Telegram delivery enabled, and bounded Codex research enabled. Three already-queued GitHub runs committed state after the merge; subsequent checks showed zero queued or in-progress runs. The pre-cutover SQLite backup passed integrity, and the former shadow configuration was retained privately as `config.shadow-20260928.toml`.

The local eBay gateway reads the account’s 5,000/day Browse allowance, meters every physical attempt, protects a 650-call reserve and limits Endgame to 3,600/day. Initial source visits are silent baselines except for listings proved to have appeared since the fresh-start epoch. The worker and dashboard are installed as LaunchAgents.

To pause local scanning and alerts, replace the private `config.toml` with `config.shadow-20260928.toml` and restart the services. Do not re-enable GitHub schedules until the local worker has stopped, all in-flight jobs have settled, and the remaining eBay quota has been checked. Retain the previous release and database backup. Never assume a Telegram send with an unknown receipt was unsent.

The initial live source check is documented in `live-cutover-2026-09-28.md`; continued uptime and reboot recovery require observation.
