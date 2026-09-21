# Profile cron mobile push: Core/Desktop phase

Named runtimes previously emitted a Desktop completion event but had no mobile
device registrations. This change keeps device credentials on the primary host.
It does not change relay code, Firestore task mirrors, mobile UI, or delivery
preferences.

## Flow

1. Cron persists the output and a small notification policy/preview snapshot in
   the existing run metadata. Transient retries, silent results, disabled
   delivery and targeted external-channel delivery remain ineligible.
2. A managed named runtime emits its existing completion event. It no longer
   independently sends device pushes. Primary and standalone gateway behavior
   stays unchanged.
3. Core's profile manager forwards completion without requiring UI subscribers.
   If Desktop manages/observes the runtime, its main process forwards only the
   socket-bound profile name and job/run IDs via `profiles.cron.notify`.
4. The primary host reads that exact retained run; caller-supplied bodies,
   destinations and credentials are never used. It does not start a scheduler or
   load/write another process's jobs.json. Missing/expired/purged output and old
   metadata without a notification snapshot are not sent.
5. An exclusive archive-local `.mobile-push` claim prevents duplicate attempts
   across both observers, reconnects and host restarts. The primary's existing
   relay push sender adds each device's gateway/server routing. Payloads include
   `profileHostId`, `profileBotId`, `profileName`, `jobId` and `runId`.

Desktop retains its existing native completion event. It waits for a temporary
primary disconnect using the existing bounded reconnect helper. A missing host,
reconnect timeout or older Core that rejects the new RPC is logged without
breaking the Desktop result/notification.

## Verification and limits

- 135 Core tests passed across profile push, cron lifecycle/retention, retry,
  primary push, profile host, real isolated gateway lifecycle and relay routing.
  Tests execute real CronService persistence and host RPC dispatch, including
  both simulated iOS/gateway and Android/relay registrations, concurrent observer
  deduplication and host restart. Only the outbound HTTP push transport is mocked
  in the new delivery tests. The existing Pydantic deprecation warning remains.
- Desktop: 56 tests passed across supervisor, IPC and schedule/profile adapters;
  main/preload typecheck and Electron build passed. Tests cover source identity,
  duplicate listener binding, reconnection, old-host failure and local event
  preservation.
- No device notification, mobile UI or installed binary was exercised/updated.
  The next acceptance step is an iOS and Android background-app notification from
  a named profile, followed by tapping into that profile's exact retained run.
- This retains best-effort push semantics, not a durable retry queue. The claim
  limits dispatch attempts; it is not proof that APNs/FCM delivered a banner.
  Network failure or a host crash during sending can still lose a notification.
- Run both the primary and managed profiles with the updated Core; update
  Desktop too when it owns the profile runtimes. Already-running/packaged older
  runtimes do not acquire these changes from a source checkout edit.

Logs: `/tmp/profile-cron-core-regression.log`,
`/tmp/profile-cron-desktop-final.log`, `/tmp/profile-cron-desktop-build.log`.
No merge, push, deployment, live task mutation or service restart in this phase.
