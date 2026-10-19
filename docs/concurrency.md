# Concurrent use

What this deployment does when several people compile at once, and what it
refuses to do.

## It renders two documents at a time, and queues the rest

```bash
curl -s $SVC/health | jq .capacity
# {"max_renders": 2, "max_queued": 8, "rendering": 2, "waiting": 3, "admitted": 5}
```

Two, because the allocation has two CPUs. Raising it does not raise throughput —
that was measured, not assumed. On 2 CPU / 4 GiB:

| concurrent | per job | ×single | throughput | peak RSS |
|---|---|---|---|---|
| 1 | 4.0 s | 1.0 | 0.23 job/s | 218 MiB |
| 2 | 5.5 s | 1.4 | 0.31 job/s | 321 MiB |
| 4 | 12.7 s | 3.2 | 0.31 job/s | 500 MiB |
| 8 | 26.7 s | **6.7** | 0.28 job/s | 1.19 GiB |

The CPU is saturated at two. Everything past that is queueing — and before
`ABCPRINT_MAX_RENDERS` existed it queued inside the kernel scheduler, where the
wait is invisible and shared out equally. Eight concurrent builds each took 6.7×
as long for about 20% more total throughput: nobody got a document sooner and
everybody waited longer.

Memory is the harder limit. A concurrent full-thesis render costs ~674 MiB, so
4 GiB is exhausted at about six. The old admission bound was anyio's default
thread limiter — **40** — on a single replica, so an OOM kill would have taken
every in-flight build with it.

## A full queue is a 429, not a long wait

```
HTTP 429  Retry-After: 30
{"detail":"10 builds are already admitted (2 rendering, 8 may wait). Retry
 shortly — this deployment renders 2 at a time because that is how many CPUs it
 has, and admitting more makes every build slower without finishing any sooner."}
```

Refusing on the request path is deliberate. A caller who cannot be served should
learn it now rather than from a four-minute poll.

**A queued job reports `queued`.** Previously every one of eight concurrent builds
reported `running`, so the service looked slow rather than busy. `started` is set
when the render actually begins, so `duration_s` measures rendering and not
waiting.

| variable | default | |
|---|---|---|
| `ABCPRINT_MAX_RENDERS` | 2 | concurrent renders; match the CPUs |
| `ABCPRINT_MAX_QUEUED` | 8 | how many may wait before 429 |

A value below 1, or not an integer, is fatal at startup rather than silently
replaced by the default.

## Deliveries are namespaced per token

`downloads` writes under a folder named for your token; a `minio` key must begin
with `users/<your token label>/`. Neither was true before 2026-10-19 — two tokens
delivering the same filename wrote the same path and the second silently replaced
the first.

A key outside your prefix is **refused**, not relocated: a rewritten key would
mean the `uri` in the response is not the one you asked for, and you could not
tell that from success. `ABCPRINT_S3_BUCKET`, when set, is the only bucket any
caller may name.

A `subdir` must be a single path component. `a/b` used to become `b` — contained,
but not what was asked for, and two callers wanting different nested folders would
have collided in one.

## Artefacts expire; jobs do not

A finished job's workspace is reclaimed `ABCPRINT_ARTIFACT_TTL_S` after it
finishes (6 hours by default), and immediately when the job is evicted past the
200-job store limit. Download what you need inside the window, or use
`POST /jobs/{id}/deliver`.

The **job** survives: an old id still returns its state, its check report and its
manifest. It is the files that go, and the artefact endpoint then answers `410`
rather than `404`, because "it existed and has expired" and "it never existed" are
different answers. Before this, nothing was ever reclaimed — `rmtree` appeared
only on error paths, four full-thesis builds left 148 MB behind, and eviction
dropped the only record of where the directory was.

A render in flight is never swept.

## What is still true

- **One replica, in-process job store.** A second replica would not recognise the
  first's job ids. Two cores and a semaphore are the constraint today, not the
  store; a shared store is the change to make when the CPU budget grows.
- **No per-token quota.** The queue is global, so one token can fill it. Bounded
  and visible now, but not fair.
