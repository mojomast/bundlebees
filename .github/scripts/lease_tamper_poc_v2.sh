#!/usr/bin/env bash
# lease_tamper.py adversarial reproduction, v2.
# Builds a base branch with an APPROVED contract (.rizzstack/contracts/<sha>.json) and a matching
# frozen lease, so each case exercises the real slice path instead of failing early as "non-slice".
# Usage: bash lease_tamper_poc_v2.sh /path/to/lease_tamper.py
# Prints: case, expected exit, actual exit, PASS/FAIL, and the gate's reason line.
set -u
GATE_SRC="${1:?path to lease_tamper.py}"
GATE_SRC="$(cd "$(dirname "$GATE_SRC")" && pwd)/$(basename "$GATE_SRC")"
T=$(mktemp -d); cd "$T"; git init -q -b main r; cd r
git config user.email t@t; git config user.name t
G=.github/scripts/lease_tamper.py
mkdir -p .github/scripts .rizzstack/leases .rizzstack/contracts src tests
cp "$GATE_SRC" "$G"
echo 'x=1' > src/app.py; echo 'def test(): pass' > tests/test_frozen.py
git add -A; git commit -qm base; SRC=$(git rev-parse HEAD)

# write_contract <name> <source_rev> <assigned-json-list> -> prints sha; writes contract + lease
write_pair() {
  python3 - "$@" <<'PY'
import json, hashlib, sys, os
name, src, assigned, branch, review_lease = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4], sys.argv[5]
scope = {"assigned_files": assigned, "test_files": ["tests/test_frozen.py"], "prohibited": [".github/**"], "read_only": []}
body = {"id": name, "source_rev": src, "scope": scope}
sha = hashlib.sha256(json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
contract = dict(body, review={"status": "approved", "approved_by": "owner"})
json.dump(contract, open(".rizzstack/contracts/%s.json" % sha, "w"))
lease = {"id": name, "branch": branch, "source_rev": src, "contract_sha256": sha, "scope": scope}
if review_lease == "yes":
    lease["review"] = {"status": "approved", "approved_by": "owner"}
json.dump(lease, open(".rizzstack/leases/%s.json" % name, "w"))
print(sha)
PY
}
SHA_A=$(write_pair slice-a "$SRC" '["src/app.py"]' feat/slice-a yes)
SHA_G=$(write_pair slice-glob "$SRC" '["src/*"]' feat/slice-glob yes)
SHA_N=$(write_pair slice-noreview "$SRC" '["src/app.py"]' feat/slice-noreview no)
SHA_W=$(write_pair slice-wide "$SRC" '["src/app.py"]' feat/slice-wide yes)
# widen slice-wide's LEASE beyond its contract (contract stays narrow)
python3 - <<'PY'
import json; p=".rizzstack/leases/slice-wide.json"; d=json.load(open(p)); d["scope"]["assigned_files"]=["src/app.py","tests/test_frozen.py"]; json.dump(d,open(p,"w"))
PY
SHA_M=$(write_pair slice-mut "$SRC" '["src/app.py"]' feat/slice-mut yes)
# mutate slice-mut's CONTRACT text after approval (hash in name no longer matches)
python3 - "$SHA_M" <<'PY'
import json,sys; p=".rizzstack/contracts/%s.json"%sys.argv[1]; d=json.load(open(p)); d["scope"]["assigned_files"].append("README.md"); json.dump(d,open(p,"w"))
PY
python3 - <<'PY'
import json; p=".rizzstack/leases/slice-mut.json"; d=json.load(open(p)); d["scope"]["assigned_files"].append("README.md"); json.dump(d,open(p,"w"))
PY
printf '{"id":"gate","source_rev":"%s","review":{"status":"approved","approved_by":"owner"},"scope":{"assigned_files":[".github/scripts/lease_tamper.py"]}}\n' "$SRC" > .rizzstack/leases/gate.json
git add -A; git commit -qm "approved contracts + leases on base"; BASE=$(git rev-parse HEAD)

n=0; fails=0
check() { # check <label> <expected> <args...>
  local label="$1" exp="$2"; shift 2
  local out; out=$(python3 "$G" --repo . "$@" 2>&1); local code=$?
  local why; why=$(echo "$out" | grep -E "FAILED|OK:" | head -1 | cut -c1-170)
  local verdict=PASS; [ "$code" = "$exp" ] || { verdict=FAIL; fails=$((fails+1)); }
  n=$((n+1)); printf '%-62s expect=%s got=%s %s\n    %s\n' "$label" "$exp" "$code" "$verdict" "$why"
}
pr() { git checkout -q main; git checkout -qb "$1"; }

pr feat/slice-a; echo 'x=2' > src/app.py; git commit -qam "in-lease change"; H=$(git rev-parse HEAD)
check "P. positive control: clean in-lease change" 0 --base $BASE --head $H --branch feat/slice-a --pull-request 1

git checkout -q main; git branch -qD feat/slice-a; pr feat/slice-a
echo 'def test(): assert True' > tests/test_frozen.py; git commit -qam "edit frozen test"; H=$(git rev-parse HEAD)
check "0. frozen test edited (must fail on scope, not non-slice)" 1 --base $BASE --head $H --branch feat/slice-a --pull-request 1
check "1. same + body 'Lease-Verify: <head>'" 1 --base $BASE --head $H --branch feat/slice-a --pull-request 1 --body "Lease-Verify: $H"
check "1b. same + label 'lease-verify:<head>'" 1 --base $BASE --head $H --branch feat/slice-a --pull-request 1 --labels "lease-verify:$H"

pr chore/anything; printf '{"id":"self","source_rev":"%s","scope":{"assigned_files":["src/app.py",".github/scripts/lease_tamper.py"]}}\n' $BASE > .rizzstack/lease.json
echo '# gutted' >> $G; git add -A; git commit -qm "self lease + gate edit"; H=$(git rev-parse HEAD)
check "2. PR adds own .rizzstack/lease.json + edits gate" 1 --base $BASE --head $H --branch chore/anything --pull-request 2

git checkout -q main; git branch -qD feat/slice-a; pr feat/slice-a
echo 'x=3' > src/app.py; printf '{"id":"grant","branch":"evil/x","scope":{"assigned_files":["src/app.py"]}}\n' > .rizzstack/leases/grant.json
git add -A; git commit -qm "app change + sneak lease"; H=$(git rev-parse HEAD)
check "3. slice PR sneaks a new lease file" 1 --base $BASE --head $H --branch feat/slice-a --pull-request 3

pr fix/whatever; echo '# weakened' >> $G; git commit -qam "weaken gate"; H=$(git rev-parse HEAD)
check "4. body 'Lease-Contract:' picks the gate lease" 1 --base $BASE --head $H --branch fix/whatever --pull-request 4 --body "Lease-Contract: .rizzstack/leases/gate.json"
check "4b. label 'lease-contract:' picks the gate lease" 1 --base $BASE --head $H --branch fix/whatever --pull-request 4 --labels "lease-contract:.rizzstack/leases/gate.json"
check "4c. branch named 'gate' (leaf matches gate lease id)" 1 --base $BASE --head $H --branch evil/gate --pull-request 4

pr feat/slice-glob; echo 'x=4' > src/app.py; git commit -qam "glob slice change"; H=$(git rev-parse HEAD)
check "5. approved contract with glob scope 'src/*'" 1 --base $BASE --head $H --branch feat/slice-glob --pull-request 5

pr feat/slice-noreview; echo 'x=5' > src/app.py; git commit -qam "no-review lease"; H=$(git rev-parse HEAD)
check "6. lease with no review block (must not count as frozen)" 1 --base $BASE --head $H --branch feat/slice-noreview --pull-request 6

pr feat/slice-wide; echo 'def test(): assert 1' > tests/test_frozen.py; git commit -qam "uses widened lease"; H=$(git rev-parse HEAD)
check "7. lease wider than its approved contract" 1 --base $BASE --head $H --branch feat/slice-wide --pull-request 7

pr feat/slice-mut; echo 'readme' > README.md; git add -A; git commit -qm "uses mutated contract"; H=$(git rev-parse HEAD)
check "8. contract text changed after approval (hash mismatch)" 1 --base $BASE --head $H --branch feat/slice-mut --pull-request 8

echo; echo "$n cases, $fails unexpected"