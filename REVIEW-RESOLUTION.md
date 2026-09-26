# Windows Scroll Linux reliability review

The pre-release review identified twelve reproducible defects. The public release uses a Rust input daemon and a separate, unprivileged Python desktop helper. This document records the corrections; it is not a claim that all possible defects have been eliminated.

| Finding | Correction | Regression coverage |
| --- | --- | --- |
| F01: unrelated applications matched | Require exact process identity or a verified Flatpak instance and accessibility proxy. | Ordinary siblings, unrelated instances, spoofed wrappers and exact identity. |
| F02: duplicate titles matched | Require a unique matching window in both KWin and accessibility data, with matching geometry. | Duplicate titles, hidden or missing accessibility windows, geometry mismatch. |
| F03: stale pointer within an input report | Flush prior movement; veto native actions after recent motion, same-report motion or motion during inspection, including other mice. Recheck the desktop target. | Engine movement reversals and native vetoes; runtime tests exercise report ordering. |
| F04: reconnect/attachment never retried | Track live device ownership and node identity; retry failed attachments with bounded delay. | Device identity replacement and isolated device lifecycle checks. |
| F05: wrong session authorized | Resolve the active local Wayland session for seat0 through logind; authenticate peer credentials; route state to one authorized helper. | Wrong UID/session/seat, remote and non-graphical sessions. Real multi-user switching remains a manual validation case. |
| F06: late decisions after cancellation | Bind decisions to request, peer, epoch and focus; invalidate atomically on pause, lock, disconnect and session changes. | Delayed decisions, pause races, cancellation and stale generations. |
| F07: inspection stalls input | Keep reading input independently; enforce an absolute helper deadline; kill and replace hung accessibility workers. | Hanging/slow workers, worker reuse and deadline enforcement. |
| F08: lost release swallows next click | Reconcile forwarded and suppressed button states with the kernel after dropped reports. | Missing releases, held buttons, fresh clicks and balanced output. |
| F09: numeric overflow leaves capture stuck | Bound configuration and arithmetic; supervise the single critical event loop with systemd readiness and watchdog. | Nonfinite/extreme values and generated event sequences. |
| F10: misleading settings | Remove unsupported controls; implement indicator visibility; validate through the same Rust parser before saving. | Supported settings, legacy compatibility and invalid assignments. |
| F11: rollback blocks retry | Versioned releases, atomic activation, exact managed-file backups and durable transaction recovery. | Forced failure, successful retry, interrupted update, cross-filesystem staging, ownership/mode/xattr restoration. |
| F12: second mouse does not cancel | Cancel globally on mouse buttons and physical wheels; route other mouse motion to the active scroll anchor. | Cross-device motion, held-button chords and cancellation. |

Additional fixes include an exclusive daemon lock, bounded socket queues, refusal to attach devices while buttons are held, discarding pre-grab input, safe long device names, and removal of idle busy polling.

A final concurrency review reproduced a settings heartbeat overwriting a completed enable/pause action with an older disk snapshot. Configuration reads and writes now share a dedicated lock with consistent lock ordering. Five deterministic regressions cover both directions, concurrent toggles, shutdown, and responsive focus cancellation during disk I/O.

Native middle-clicks are still a conservative semantic decision. An application can change content after inspection or expose inaccurate accessibility data. Exact Windows behavior and an absolute impossibility of paste cannot honestly be guaranteed while preserving native middle-click actions. Unknown targets receive scrolling or suppression; they do not receive generic native middle-click fallback. Excluded devices and periods when the service is stopped are outside interception.

Scope excludes a full security audit of the operating system, KDE, GTK, system Python libraries and the private layer-shell library. The Cargo lockfile was checked against RustSec database revision `e2111519ba6d14a5da59a7b2e5c8083ae8a37c01`: the only package-name matches were old bitvec and nix advisories; the locked versions are within their patched ranges. This was a direct database review, not a `cargo audit` run. See https://rustsec.org/advisories/.
