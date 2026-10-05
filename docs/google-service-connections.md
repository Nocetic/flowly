# Independent Google service connections

The existing authenticated `gmail.*` transport is retained. Clients must check
`gmail.capabilities.independentServices === true` before sending `service` on
status, setup begin/pending/status/cancel or disconnect. The allowlist is `gmail`,
`calendar`, `drive`, `contacts`, `tasks`. `gmail_manage` is a permission in the
Gmail connection, never a connection identifier. Calls omitting `service` retain
the legacy bundle contract; legacy CLI management still addresses that bundle.

Each service stores its private grant in `credentials/google-{service}.json` and
its resumable setup/replacement journal in `google-{service}-setup.json` within
the selected profile. Tools resolve the service record first. File writes are
atomic and private; all services and native refresh use one reentrant file lock
to serialize changes to legacy shared state. The minimum filelock version covers
the singleton/reentrancy API used by this path.

New standalone non-Gmail grants request only their service scopes plus `openid`
and `email`. Both broker and runtime verify Google's userinfo response, verified
email and stable `sub`. Gmail retains its direct mailbox profile verification.
Replacing a connection binds to the saved account and connection ID. A reply
for another service/request cannot commit or cancel this setup.

Legacy `gmail.json` is a fallback only for granted, non-disabled services. A
service disconnect writes a local stop marker before network cleanup, then adds
the service to `disabled_services` in the shared record. The last service retires
the shared broker grant. An outage preserves cleanup credentials for retry.
Replacing a shared record commits the new service first, then detaches that
service through the journal; later disconnect cannot resurrect its old fallback.
Native historical credentials without recorded scopes retain their compatibility
path for each remaining service. Tokens may still exist outside Flowly; local
disconnect is not Google's account-wide token revocation.

Chat proposals bind one service to the originating conversation. The UI starts
OAuth only on a user action. Profile-host allowlists include Google chat discovery
and cancellation; existing owner, profile identity and secure transport guards
continue to apply. Google tool availability no longer depends on Gmail being
enabled. Sending/writing retains the existing approval behavior.

Roll out the companion web broker before this runtime, then Desktop. No migration
sweeps or Google project-wide revocation are required. Older runtimes receive an
update instruction from the new service UI. Old service-omitting clients continue
to use the legacy bundle and do not manage new per-service records.

Verification: `tests/test_google_service_connections.py` exercises independent
authorization, identity/scopes, isolated cancellation/disconnect, outage retry,
legacy migration and chat binding using fixture HTTP. The profile-host runtime
integration test exercises real child-process routing. The web repository's
`scripts/test-gmail-runtime-contract.ts` tests the real Python service against the
TypeScript broker with fixture Google responses. No live account is used.
