set quiet
set minimum-version := '1.55.1'
set default-list
set default-script
set shell := ['bash', '-euo', 'pipefail', '-c']
set script-interpreter := ['bash', '-euo', 'pipefail']

# Rendering implementation, not a second public command hierarchy.
[private]
mod template 'template'

[private]
log lvl msg *args:
    gum log -t rfc3339 -s -l "{{ lvl }}" "{{ msg }}" {{ args }}

config_file := env('ANYLINGS_CONFIG', justfile_dir() / 'cluster.toml')
output_dir := env('ANYLINGS_OUTPUT_DIR', justfile_dir() / '.local/rendered')

[doc('Initialize platform inputs and age identity')]
init domain:
    [ {{ quote(domain) }} = platform ] || { echo 'usage: just init platform' >&2; exit 2; }
    just template init

[doc('Render, encrypt and validate platform configuration')]
configure domain:
    [ {{ quote(domain) }} = platform ] || { echo 'usage: just configure platform' >&2; exit 2; }
    just template configure

[doc('Configure the platform and render its private bootstrap install set')]
render domain:
    [ {{ quote(domain) }} = platform ] || { echo 'usage: just render platform' >&2; exit 2; }
    just configure platform
    bash "{{ output_dir }}/bootstrap/bootstrap.sh" render

[doc('Validate configuration inputs or existing rendered platform output')]
check domain:
    case {{ quote(domain) }} in
        config) uv run --quiet --locked --no-dev "{{ justfile_dir() }}/template/scripts/validate.py" "{{ config_file }}" ;;
        platform)
            just template assert-encrypted
            just template validate-references
            just template validate-kubernetes
            ;;
        *) echo 'usage: just check config|platform' >&2; exit 2 ;;
    esac

[doc('Regenerate the configuration schema')]
schema domain:
    [ {{ quote(domain) }} = config ] || { echo 'usage: just schema config' >&2; exit 2; }
    just template schema

[confirm('Remove owned generated platform output — continue? [y/N]')]
[doc('Remove owned generated platform output, never cluster resources')]
reset domain:
    [ {{ quote(domain) }} = platform ] || { echo 'usage: just reset platform' >&2; exit 2; }
    just --yes template reset

[doc('Plan infrastructure or platform changes through OpenTofu')]
plan domain:
    case {{ quote(domain) }} in
        k0s|doks|cloudflare|platform) bash "{{ justfile_dir() }}/infra/tofu/run.sh" plan {{ quote(domain) }} ;;
        *) echo 'usage: just plan k0s|doks|cloudflare|platform' >&2; exit 2 ;;
    esac

[doc('Apply infrastructure or GitOps roots through OpenTofu')]
apply domain:
    case {{ quote(domain) }} in
        k0s-storage) bash "{{ justfile_dir() }}/infra/k0s/storage.sh" ;;
        k0s|doks|cloudflare|platform) bash "{{ justfile_dir() }}/infra/tofu/run.sh" apply {{ quote(domain) }} ;;
        *) echo 'usage: just apply k0s|k0s-storage|doks|cloudflare|platform' >&2; exit 2 ;;
    esac

[confirm('Destroy the selected infrastructure — continue? [y/N]')]
[doc('Destroy infrastructure; does not remove external application-volume data')]
destroy domain:
    case {{ quote(domain) }} in
        k0s|doks|cloudflare|platform) bash "{{ justfile_dir() }}/infra/tofu/run.sh" destroy {{ quote(domain) }} ;;
        *) echo 'usage: just destroy k0s|doks|cloudflare|platform' >&2; exit 2 ;;
    esac

[doc('Print issued kubeconfig; redirect to a private file, never to logs')]
export domain:
    case {{ quote(domain) }} in
        k0s|doks) bash "{{ justfile_dir() }}/infra/tofu/run.sh" export {{ quote(domain) }} ;;
        *) echo 'usage: just export k0s|doks' >&2; exit 2 ;;
    esac

[doc('First-install Argo using the generated bootstrap; never an upgrade')]
bootstrap domain:
    [ {{ quote(domain) }} = platform ] || { echo 'usage: just bootstrap platform' >&2; exit 2; }
    bash "{{ output_dir }}/bootstrap/bootstrap.sh" install

[doc('Wait for provider readiness or the initial Argo installation')]
wait domain:
    case {{ quote(domain) }} in
        cluster) bash "{{ justfile_dir() }}/infra/platform/wait-cluster.sh" ;;
        platform) bash "{{ output_dir }}/bootstrap/bootstrap.sh" wait ;;
        *) echo 'usage: just wait cluster|platform' >&2; exit 2 ;;
    esac
