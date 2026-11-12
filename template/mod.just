set quiet
set default-list
set default-script
set shell := ['bash', '-euo', 'pipefail', '-c']
set script-interpreter := ['bash', '-euo', 'pipefail']
set working-directory := '..'

justfile_dir := justfile_dir()
template_dir := justfile_dir + '/template'
template_resources := template_dir + '/resources'
scripts_dir := template_dir + '/scripts'

# Isolated inputs and outputs: no generated file is written to the repository
# root, and different instances use different output roots.
config_file := env('ANYLINGS_CONFIG', justfile_dir + '/cluster.toml')
output_dir := env('ANYLINGS_OUTPUT_DIR', justfile_dir + '/.local/rendered')
schema_file := justfile_dir + '/cluster.schema.json'
sample_config_file := justfile_dir + '/cluster.sample.toml'
validate_script := scripts_dir + '/validate.py'
# Every SOPS call (just, bootstrap.sh) uses the same identity, and it defaults
# outside Git even when .mise is not loaded (CI).
export SOPS_AGE_KEY_FILE := env('SOPS_AGE_KEY_FILE', justfile_dir + '/.local/age.key')

[doc('Render and validate configuration files')]
[group('template')]
configure: render encrypt-secrets
    just check platform

# ANYLINGS_OUTPUT_DIR comes from the caller, so it is handled as untrusted.
# A render may only delete what it created itself: the marker plus the
# bootstrap/, kubernetes/ and .sops.yaml entries, none of which may be a
# symlink. Everything else in the directory is left untouched.
[private]
check-output-dir:
    out="{{ output_dir }}"
    case "$out" in
        "" | "/" | "~" | "~/"* | "." | "..")
            just log fatal "refusing unsafe ANYLINGS_OUTPUT_DIR" path "$out"
            ;;
    esac
    [ ! -L "$out" ] || just log fatal "refusing symlinked ANYLINGS_OUTPUT_DIR" path "$out"
    [ ! -L "$(dirname "$out")" ] || just log fatal "refusing ANYLINGS_OUTPUT_DIR under a symlink" path "$out"
    resolved="$(realpath -m -- "$out")"
    # Refuse a render that would own (contain or equal) a protected path: the
    # recipe deletes bootstrap/, kubernetes/ and .sops.yaml below the output
    # dir, so an alias of the sources or of the key would delete them.
    for protected in \
        "{{ justfile_dir }}/template/config" \
        "$(realpath -m -- "{{ config_file }}")" \
        "$(realpath -m -- "${SOPS_AGE_KEY_FILE:-{{ justfile_dir }}/.local/age.key}")" \
        "${HOME:-/nonexistent}"
    do
        p="$(realpath -m -- "$protected")"
        case "$p/" in
            "$resolved/"*) just log fatal "refusing output dir that contains a protected path" path "$resolved" protected "$p" ;;
        esac
        [ "$p" != "$resolved" ] \
            || just log fatal "refusing output dir equal to a protected path" path "$resolved"
    done
    # Refuse an output dir inside the template sources: generated names would
    # shadow tracked files.
    case "$resolved/" in
        "$(realpath -m -- "{{ template_dir }}")/"*)
            just log fatal "refusing output dir inside template sources" path "$resolved"
            ;;
    esac
    if [ -e "$out" ]; then
        marker="$out/.anylings-rendered"
        [ -d "$out" ] || just log fatal "ANYLINGS_OUTPUT_DIR is not a directory" path "$out"
        if [ -e "$marker" ] || [ -L "$marker" ]; then
            { [ -f "$marker" ] && [ ! -L "$marker" ]; } \
                || just log fatal "render marker is not a regular file" path "$marker"
        elif [ -n "$(find "$out" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
            just log fatal "refusing non-empty directory not created by render" path "$resolved"
        fi
        for generated in bootstrap kubernetes .sops.yaml; do
            [ ! -L "$out/$generated" ] \
                || just log fatal "refusing to overwrite symlink" path "$out/$generated"
        done
    fi

[doc('Render manifests into ANYLINGS_OUTPUT_DIR (no encryption or cluster access)')]
[group('template')]
render: check-output-dir
    # umask covers makejinja itself: rendered values are plaintext until
    # `encrypt-secrets` runs, so they must never be group/other readable.
    umask 077
    out="{{ output_dir }}"
    if [ -f "$out/.anylings-rendered" ]; then
        rm -rf "$out/bootstrap" "$out/kubernetes" "$out/.sops.yaml"
    fi
    mkdir -p "$out"
    chmod 700 "$out"
    PYTHONDONTWRITEBYTECODE=1 uv run --locked --no-dev makejinja \
        --input "{{ justfile_dir }}/template/config" \
        --output "$out" \
        --import-path "{{ scripts_dir }}" \
        --force
    chmod +x "$out/bootstrap/bootstrap.sh"
    : > "$out/.anylings-rendered"

# Creates only what is missing, and never overwrites an existing key: a
# re-run must not invalidate already-encrypted files. Paths given by the
# caller are honoured, including their parent directories.
[doc('Initialize isolated inputs (cluster.toml, age key, deploy key)')]
[group('template')]
init:
    config_file="{{ config_file }}"
    age_key_file="${SOPS_AGE_KEY_FILE:-{{ justfile_dir }}/.local/age.key}"
    # Never chmod the parent: SOPS_AGE_KEY_FILE may live in $HOME or in the
    # repository, and this recipe must not change directory modes it did not
    # create.
    mkdir -p "$(dirname "$config_file")" "$(dirname "$age_key_file")"
    # The sample is a valid starting point that names a tunnel token file but
    # holds no credential; supply the token before configuring a deployment.
    [ -f "$config_file" ] || cp "{{ sample_config_file }}" "$config_file"
    if [ ! -s "$age_key_file" ]; then
        (umask 077 && age-keygen --output "$age_key_file")
    fi
    chmod 600 "$age_key_file"
    # A deploy key is only needed for ssh:// repository URLs; generate it next
    # to the config so separate instances keep separate keys.
    if just template config-json 2>/dev/null \
        | jq -e '.repository.url | startswith("ssh://")' >/dev/null; then
        deploy_key_file="${ARGOCD_DEPLOY_KEY_FILE:-$(dirname "$config_file")/deploy.key}"
        if [ ! -s "$deploy_key_file" ]; then
            (umask 077 && ssh-keygen -t ed25519 -C "anylings-deploy-key" -f "$deploy_key_file" -q -N "")
        fi
        chmod 600 "$deploy_key_file"
    fi

# Editors resolve the #:schema directive in cluster.toml against this file.
[doc('Regenerate the cluster.toml JSON Schema from the validator')]
[group('template')]
schema:
    uv run --quiet --locked --no-dev "{{ validate_script }}" --schema > "{{ schema_file }}"
    oxfmt "{{ schema_file }}" > /dev/null

[confirm("Remove all rendered files — continue? [y/N]")]
[doc('Remove the rendered output directory')]
[group('template')]
reset: check-output-dir
    out="{{ output_dir }}"
    if [ -f "$out/.anylings-rendered" ]; then
        rm -rf "$out/bootstrap" "$out/kubernetes" "$out/.sops.yaml" "$out/.anylings-rendered"
    fi

# Validated, defaulted cluster config as JSON on stdout; fails when the
# config is invalid.
[private]
config-json:
    uv run --quiet --locked --no-dev "{{ validate_script }}" "{{ config_file }}"

# `sops encrypt` is idempotent: files already encrypted for the configured
# recipient (e.g. re-rendered on another machine) are left alone.
[private]
encrypt-secrets:
    find "{{ output_dir }}/bootstrap" "{{ output_dir }}/kubernetes" -type f -name '*.sops.*' -print0 \
        | while IFS= read -r -d '' f; do
            if ! status="$(sops --config "{{ output_dir }}/.sops.yaml" filestatus "$f" | jq -r '.encrypted')"; then
                just log fatal "could not read encryption status" file "$f"
            fi
            case "$status" in
                false) sops --config "{{ output_dir }}/.sops.yaml" encrypt --in-place "$f" ;;
                true)  : ;;
                *)     just log fatal "could not determine encryption status" file "$f" ;;
            esac
        done

# Nothing sensitive may stay in plaintext. SOPS encrypts values, not keys, so
# `stringData:` and `TUNNEL_TOKEN:` stay readable by design; what must never
# appear is a readable value. Check both SOPS metadata and every nonempty
# scalar, because metadata alone accepts partially encrypted files.
[private]
assert-encrypted:
    found=0
    while IFS= read -r -d '' f; do
        found=1
        if [ "$(sops --config "{{ output_dir }}/.sops.yaml" filestatus "$f" | jq -r '.encrypted')" != "true" ]; then
            just log fatal "not encrypted" file "$f"
        fi
        # Inspect every nonempty scalar outside SOPS metadata, including
        # Helm values. filestatus alone accepts partially encrypted files.
        if ! yq -o=json '.' "$f" | jq -e '
            del(.sops) | [.. | scalars | select(. != null and . != "")
              | select(if type == "string" then startswith("ENC[") | not else true end)]
            | length == 0' >/dev/null; then
            just log fatal "plaintext value remains in encrypted file" file "$f"
        fi
    done < <(find "{{ output_dir }}/bootstrap" "{{ output_dir }}/kubernetes" -type f -name '*.sops.*' -print0)
    [ "$found" = 1 ] || just log fatal "no encrypted files were rendered"

[private]
validate-kubernetes:
    bash "{{ template_resources }}/kubeconform.sh" "{{ output_dir }}/kubernetes"

# Argo CD resolves these paths against the published Git tree, which is only
# the `kubernetes/` subtree of the render. A reference that leaves that subtree,
# or that only exists outside it, cannot reconcile even though the file is
# present on this machine.
[private]
validate-references:
    prefix="$(just template config-json | jq -r '.repository.path')"
    prefix="${prefix%/}"
    published="{{ output_dir }}/kubernetes"
    [ -d "$published" ] || just log fatal "rendered kubernetes tree is missing" path "$published"
    published="$(realpath -e -- "$published")"
    while IFS= read -r -d '' config; do
        for key in valuesFile manifestPath; do
            ref="$(jq -r --arg k "$key" '.[$k] // ""' "$config")"
            [ -n "$ref" ] || continue
            case "$ref" in
                /*) just log fatal "unusable $key: absolute path" app "$config" path "$ref" ;;
            esac
            case "$ref" in
                *\\*) just log fatal "unusable $key: backslash" app "$config" path "$ref" ;;
            esac
            if [ -n "$prefix" ]; then
                case "$ref" in
                    "$prefix"/*) rel="${ref#"$prefix/"}" ;;
                    *) just log fatal "unusable $key: missing the repository.path prefix" app "$config" path "$ref" expected_prefix "$prefix" ;;
                esac
            else
                rel="$ref"
            fi
            case "$rel/" in
                kubernetes/*) ;;
                *) just log fatal "unusable $key: outside the published tree" app "$config" path "$ref" ;;
            esac
            case "/$rel/" in
                */../*) just log fatal "unusable $key: parent segment" app "$config" path "$ref" ;;
                */./*) just log fatal "unusable $key: current segment" app "$config" path "$ref" ;;
                *//*) just log fatal "unusable $key: empty segment" app "$config" path "$ref" ;;
            esac
            target="{{ output_dir }}/$rel"
            if [ "$key" = manifestPath ]; then
                [ -d "$target" ] || just log fatal "missing $key" app "$config" path "$rel"
            else
                [ -f "$target" ] || just log fatal "missing $key" app "$config" path "$rel"
            fi
            # A symlink can satisfy the containment test above while resolving
            # elsewhere, so check the real target, not the name.
            real="$(realpath -e -- "$target")" \
                || just log fatal "unresolvable $key" app "$config" path "$rel"
            case "$real/" in
                "$published/"*) ;;
                *) just log fatal "unusable $key: resolves outside the published tree" app "$config" path "$rel" resolved "$real" ;;
            esac
        done
    done < <(find "$published/apps" -mindepth 3 -maxdepth 3 -type f -name config.json -print0)
    just log info "application references resolve inside the published tree"
