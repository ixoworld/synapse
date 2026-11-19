# IXO

## Customizations

All customization tags MUST map to the upstream tag they are based on.
All ixo tags MUST be prefixed with `ixo` and MUST be suffixed with own version counter `.1` since we can have multiple versions of ixo customizations per upstream release. (eg: `v1.119.0-ixo.1`)
Please list changes below based of which ixo tag version the customization will be applied.

### IXO patches (what we changed, and where)

Everything that differs from upstream, apart from this file. Since v1.165.0-ixo.2 every
patched spot in the code carries a marker comment, so `git grep -n "IXO-PATCH("` lists
them all (the older PDU-size patch is marked with `IXO:` / the comment shown below).
To see the full fork diff: `git diff <upstream tag> <ixo tag>`, for example
`git diff v1.165.0 v1.165.0-ixo.2 --stat`.

| # | Patch (marker) | Files and functions | What and why | Upstream refs |
|---|---|---|---|---|
| 1 | PDU size 10 MiB | `synapse/api/constants.py` `MAX_PDU_SIZE`; `rust/src/events/constants.rs` `MAX_PDU_SIZE_BYTES` | Events up to 10 MiB instead of 64 KiB, for large ai-oracle events. Keep the Python and Rust values in sync. Side effect: `MAX_REQUEST_SIZE = 200 * MAX_PDU_SIZE` becomes 2 GiB. | none (IXO-only) |
| 2 | `IXO-PATCH(as-self-heal)` | `synapse/appservice/scheduler.py`: `RECOVER_DOWN_SERVICES_INTERVAL`, `ApplicationServiceScheduler.__init__` (looping call), `.start()`, `.start_recoverers_for_down_services()`, `_TransactionController.send()`, `.start_recoverer()`, `.is_recovering()` | An appservice marked `down` in `application_services_state` recovers by itself. Upstream only retries from the process whose send failed, and only scans for `down` services once at scheduler start. When another process writes `down` later (on Nov 18 2026: the old appservice pod during a rolling restart), nobody ever retried: the mainnet rooms appservice stayed down 8.5 h. Now: `send()` starts a recoverer when the service is `down` and this process has none; every 60 s the sending process (`notify_appservices_from_worker` / `should_notify_appservices` only) starts recoverers for `down` services that lack one; `start_recoverer()` is idempotent. No config. | [#20342](https://github.com/element-hq/synapse/issues/20342), [#20454](https://github.com/element-hq/synapse/issues/20454), PR [#20458](https://github.com/element-hq/synapse/pull/20458) (idempotent `start_recoverer` only) |
| 3 | `IXO-PATCH(as-to-device)` | `synapse/handlers/appservice.py`: TO_DEVICE branch of `_notify_interested_services_ephemeral`, `notify_to_device_catch_up()`, `_handle_to_device_messages()`, `_get_to_device_messages()`; `synapse/appservice/scheduler.py`: `ApplicationServiceScheduler.is_recovering()`, `.get_to_device_queued_position()`, `.enqueue_to_device_messages()`, `_ServiceQueuer.__init__` (3 dicts), `._send_request()`, `._store_to_device_pos()`, `._drop_undelivered_to_device_messages()`, `_TransactionController.send()` (returns `bool`), `.on_recovered()`; `synapse/storage/databases/main/deviceinbox.py`: `get_device_message_recipients()`, `get_device_messages_in_range()` | To-device messages (MSC2409/MSC4203, e.g. Olm room keys) for appservices are no longer lost when a transaction fails or the service is down. Upstream stored the appservice's `to_device_stream_id` as soon as messages were read; stored transactions keep only event IDs and the scheduler drops to-device messages in recovery mode, so they were never sent. Now the position is stored only after the transaction carrying the messages was delivered; an undelivered batch is dropped from memory and re-read from `device_inbox` (rows for appservice devices stay there), and after recovery the recoverer triggers a catch-up read. Reads look up recipients for the whole range (stream change cache, else `SELECT DISTINCT user_id`) and are bounded to about 100 messages (a stream id shared by many devices is never split); the next batch is read once the previous one is delivered. A message can be delivered twice (e.g. the first 100 of a batch whose second transaction failed); the receiving OlmMachine rejects replays. No schema change. Unchanged: typing/receipts/presence, MSC3202 device lists / OTK counts / fallback keys, services without `receive_ephemeral` / `de.sorunome.msc2409.push_ephemeral`, and servers without `experimental_features.msc2409_to_device_messages_enabled`. | [#11150](https://github.com/element-hq/synapse/issues/11150), [#20342](https://github.com/element-hq/synapse/issues/20342); TODO in `get_oldest_unsent_txn` |

Patches 2 and 3 are candidates to send upstream.

Tests for patches 2 and 3 (all marked `IXO-PATCH` in their docstrings or comments):
`tests/appservice/test_scheduler.py` (`ApplicationServiceSchedulerDownStateTestCase`,
`ApplicationServiceSchedulerToDevicePositionTestCase`, and new tests in
`ApplicationServiceSchedulerTransactionCtrlTestCase`),
`tests/handlers/test_appservice.py` (`ApplicationServicesHandlerToDeviceDeliveryTestCase`),
`tests/storage/databases/main/test_deviceinbox.py` (`DeviceInboxRangeReadTestCase`).

Known behaviour of patch 3: an appservice with no stored to-device position yet (new
service, or to-device just switched on) is sent the to-device messages still waiting in
`device_inbox` for its users, in batches (upstream sent them only for the users in the
first notification). Messages skipped before ixo.2 are not re-sent (their stream ids
are below the stored position).

#### Re-applying the patches on the next upstream merge

1. Merge the new upstream tag as described under Contributing. Conflicts will be in the
   files in the table above; `git grep -n "IXO-PATCH("` on the old ixo tag shows every
   spot to carry over.
2. Check whether upstream fixed any of it meanwhile (the issues and PR linked above,
   `CHANGES.md`, and the TODO in `get_oldest_unsent_txn`). Drop a patch when upstream has
   an equivalent; keep the tests if they still apply.
3. Run the patch tests plus the surrounding suites:
   `poetry run trial tests.appservice tests.handlers.test_appservice tests.storage.test_appservice tests.storage.databases.main.test_deviceinbox`,
   then `ruff check`, `ruff format --check` and `mypy` on the changed files.
4. Known failures caused by patch 1 (not new, same on v1.165.0-ixo.1):
   `tests.http.test_site` (request-size tests) needs more memory than a 12 GiB
   container (2 GiB max request size), and
   `tests.events.test_utils.TestMaybeUpsertEventField.test_update_not_okay*` expect the
   64 KiB event limit.

### Changelog

#### v1.113.0-ixo.1

- Increasing API `MAX_PDU_SIZE` to 10MB for handling of ai-oracles events.

#### v1.132.0-ixo.1

- Version bump with upstream changes.

#### v1.140.0-ixo.1

- Version bump with upstream changes.

#### v1.165.0-ixo.1 (2026-11-18)

- Version bump with upstream changes (merge of tag `v1.165.0`).
- Kept `MAX_PDU_SIZE = 10 * 1024 * 1024` in `synapse/api/constants.py` (upstream now also derives `MAX_REQUEST_SIZE = 200 * MAX_PDU_SIZE` there).
- Set the new Rust constant `MAX_PDU_SIZE_BYTES` in `rust/src/events/constants.rs` to 10 MiB as well (unused upstream in 1.165.0), so a later release that starts enforcing it cannot silently bring back the 64 KiB limit.
- Image built from tag `v1.165.0-ixo`:

```bash
DOCKER_BUILDKIT=1 docker build --platform=linux/amd64 -f docker/Dockerfile -t ghcr.io/ixoworld/synapse:v1.165.0-ixo.1 .
docker push ghcr.io/ixoworld/synapse:v1.165.0-ixo.1
```

#### v1.165.0-ixo.2 (2026-11-19)

Same upstream base (`v1.165.0`), same database schema (95). Rollback = image `v1.165.0-ixo.1`.

- Appservices marked `down` recover by themselves (patch 2, `IXO-PATCH(as-self-heal)`): recoverer started from `send()` and by a 60 s check in the sending process; `start_recoverer()` idempotent.
- To-device messages for appservices are no longer lost while a service is down or a transaction fails (patch 3, `IXO-PATCH(as-to-device)`): the to-device stream position is stored only after delivery, undelivered messages are re-read from `device_inbox`, reads are bounded.
- Cause: on Nov 18 2026 the mainnet rooms appservice was marked down by the old pod during a daily worker restart and stayed down 8.5 h; 87 to-device messages (Olm room keys) were skipped.
- Image built from tag `v1.165.0-ixo.2`:

```bash
DOCKER_BUILDKIT=1 docker build --platform=linux/amd64 -f docker/Dockerfile -t ghcr.io/ixoworld/synapse:v1.165.0-ixo.2 .
docker push ghcr.io/ixoworld/synapse:v1.165.0-ixo.2
```

## Contributing

Below are the steps to contribute to Ixo Synapse with your own custom configurations, or to update the existing code with latest upstream changes.

```bash
# 1. Add the upstream remote (only need to do this once)
git remote add upstream https://github.com/element-hq/synapse.git

# The below 4 steps is to keep forked repo in sync with upstream
# 2. Fetch only the develop branch and all tags from upstream
git fetch upstream develop --tags
# 3. Check out your develop branch
git checkout develop
# 4. Merge the upstream develop branch
git merge upstream/develop
# 5. Push the updated develop branch to your fork with new tags
git push origin develop --follow-tags

# 6. Check out your custom branch
git checkout ixo

# 7. Merge a specific version (tag) into your custom branch
git merge v1.113.0  # Replace with the specific version tag you want to merge

# 8. Make customizations(if have any) and commit
git add .
git commit -m "feat: add customizations"

# 8. Resolve any conflicts if they occur

# 9. Run tests and make sure everything works with your customizations

# 10. Create a new tag for your customized version
git tag -a v1.140.0-ixo -m "Synapse v1.140.0 with IXO customizations"

# 11. Push ixo custom branch and the new tags
git push origin ixo --follow-tags
# git push origin ixo
# git push origin 1.119.0-ixo

# 12. Build and push your Docker image
DOCKER_BUILDKIT=1 docker build --platform=linux/amd64 -t ghcr.io/ixoworld/synapse:v1.140.0-ixo.1 -f docker/Dockerfile .
docker push ghcr.io/ixoworld/synapse:v1.140.0-ixo.1
```

# Resources

- [Synapse](https://github.com/element-hq/synapse)
- [Ixo Synapse](https://github.com/ixoworld/synapse/tree/ixo)
- [Contribution Guidelines](https://element-hq.github.io/synapse/latest/development/contributing_guide.html)
