# Contract lane design

ApiMesh's promise: swagger.json contains every endpoint the scanned repo serves,
and nothing else. Repos that declare their API in OpenAPI documents and generate
the serving code at build time (openapi-generator, oapi-codegen, connexion) keep
their routing out of the committed source, so the code lane alone cannot see it.
The contract lane reads those documents directly. This file is the contract the
implementation and its fixtures are held to.

## The invariant

An operation enters swagger.json only when there is deterministic evidence that
this repo serves it. Ambiguity always excludes, and every exclusion is reported
in `x-apimesh-coverage` with its reason. The LLM never takes part in an
include/exclude decision; it only writes descriptions for operations that are
already in, and may label excluded residue in reports.

## Evidence tiers

1. **Direct registration** (code lane): a route the framework parser proved in
   committed source.
2. **Build-linked server generation**: a build file names the spec as input to a
   server-mode generator invocation (openapi-generator `-g spring` /
   `generatorName: spring` / `interfaceOnly` / `delegatePattern`, oapi-codegen
   server configs), and the generated output is wired into this repo's code.
   Corroboration, when present: source classes implement or delegate the
   generated symbols.
3. **Correlation only**: operationId or `implements *Api` matches with no build
   edge. Never includes on its own; produces a `candidate` entry in the report
   for a human or agent to confirm with an override.

Client-mode generator invocations (`-g java`, jersey, feign, webclient,
typescript-axios and friends) mark the spec consumed: excluded, reported.
A spec with both server and client invocations is classified per invocation,
never as one whole-file verdict.

## Candidate schema

Every potential operation, from any lane, becomes a candidate:

```json
{
  "source_id": "spec:src/main/resources/specs/orders.yaml#get /api/orders",
  "lane": "contract | code",
  "method": "GET",
  "route": "/api/orders",
  "route_shape": "GET /api/orders",
  "evidence": {"tier": 2, "build_file": "app/BUILD.bazel", "generator": "spring"},
  "eligibility_hash": "<spec content + build evidence + prover version>",
  "payload_hash": "<the operation object as authored>",
  "operation": {"...": "authored content, refs resolved and namespaced"}
}
```

`route_shape` canonicalizes path parameters to `{}` so `/users/{id}` and
`/users/{userId}` collide on purpose. Collisions between candidates from
different evidence chains are reported as conflicts, never silently
overwritten; within one chain the contract candidate owns schemas, parameters
and descriptions, and the code candidate keeps its routing conditions as
`x-apimesh-routing-conditions`.

## Path layers

Served path = controller mapping prefix (proven Spring layers only) + operation
path. The deployment base URL stays in `servers[0]` (the api_host). Property
placeholders (`${...}`) and unresolvable constants are never guessed: the
affected operations are excluded and reported as unresolved path variants.
Multi-value prefixes fan out as variants.

## Service boundary (v1)

One swagger.json per run. When two deployable targets in a monorepo claim the
same route shape, both candidates survive as a reported conflict and the
operation carries `x-apimesh-service` tags. Splitting output per service is a
recorded follow-up, not silently approximated.

## Reference resolution

`$ref` resolution is local-only: repo-confined, no URLs, no absolute paths, no
symlink escape, cycle and size limits. An operation whose reference closure
cannot be fully resolved is excluded and reported. Components are namespaced
per source spec before merging.

## Discovery scope

Contract discovery sweeps the whole repo every run with its own ignore policy:
dependency caches only (`node_modules`, `.git`, virtualenvs). The code lane's
semantic ignores (`docs`, `vendor`, `tests`) do not apply, because that is
where contracts live. Discovery is never narrowed by a previous run's profile.

## Persistence

- Parse caches: regenerable, keyed by file content and parser version.
- `repo_profile.json`: a generated report of what was found, included,
  excluded and why. Read by humans and agents, never by ApiMesh itself.
- Overrides: operator assertions. `exclude` applies unconditionally
  (fail-closed). `include` is bound to the spec locator and evidence hash and
  goes dormant when the underlying file changes. Agent suggestions cannot
  force an include.

## Kill switch

`APIMESH_INGEST_SPECS=0` disables the contract lane for a run.
