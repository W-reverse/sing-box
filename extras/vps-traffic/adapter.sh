#!/usr/bin/env bash
# Isolated compatibility layer for the upstream core.sh URL generator.
set -o pipefail

root=${SB_TRAFFIC_ROOT:-/etc/sing-box}
conf_dir=${SB_TRAFFIC_CONF_DIR:-$root/conf}
core=${SB_TRAFFIC_CORE_SH:-$root/sh/src/core.sh}
caddy_conf=${SB_TRAFFIC_CADDY_CONF:-/etc/caddy/233boy}
caddyfile=${SB_TRAFFIC_CADDYFILE:-/etc/caddy/Caddyfile}
address=${SB_TRAFFIC_ADDRESS:?SB_TRAFFIC_ADDRESS is required}

[[ -r $core ]] || { printf 'sb-traffic adapter: cannot read %s\n' "$core" >&2; exit 1; }
[[ -d $conf_dir ]] || { printf 'sb-traffic adapter: config directory missing: %s\n' "$conf_dir" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { printf 'sb-traffic adapter: jq is required\n' >&2; exit 1; }

# These are the only upstream helpers required by get/info. Never source init.sh:
# its startup probes, certificate generation and service actions are intentionally avoided.
msg() { :; }
err() { printf 'sing-box core.sh: %s\n' "$*" >&2; exit 1; }
_wget() { err 'network access is forbidden in the traffic adapter'; }
# Keep upstream GNU base64's no-wrap behavior while supporting BSD/macOS base64.
base64() {
    if [[ ${1:-} == -w && ${2:-} == 0 ]]; then
        shift 2
        if command base64 -w 0 </dev/null >/dev/null 2>&1; then
            command base64 -w 0 "$@"
        else
            command base64 "$@"
        fi
    else
        command base64 "$@"
    fi
}

 source "$core"

 records=()
 for path in "$conf_dir"/*.json; do
     [[ -f $path && ! -L $path ]] || continue
     filename=${path##*/}
     [[ $filename == dynamic-port-*-link*.json ]] && continue
     record=$( (
         # Isolate every real config: core.sh keeps protocol fields in globals.
         is_config_file=$filename
         is_conf_dir=$conf_dir
         is_sh_dir=${root}/sh
         is_core_dir=${root}
         is_core_bin=${root}/bin/sing-box
         is_core_ver=${SB_TRAFFIC_CORE_VERSION:-1.0.0}
         is_core=sing-box
         is_core_name=sing-box
         is_caddy_conf=$caddy_conf
         is_caddyfile=$caddyfile
         is_https_port=443
         is_http_port=80
         is_dont_show_info=1
         is_dont_get_ip=1
         ip=$address

         # Exact is_config_file is preselected; default JSON enumeration cannot prompt.
         get info
         [[ $is_protocol ]] || err "unsupported or unreadable config: $filename"

         # Upstream Caddy site files are `${host}:${https_port} {`; honor every
         # numeric port here instead of core.sh's legacy two-digit grep expression.
         if [[ $host && -f $caddy_conf/$host.conf ]]; then
             while IFS= read -r line; do
                 case $line in
                     "$host":*)
                         port_part=${line#"$host":}
                         port_part=${port_part%%[[:space:]]*}
                         port_part=${port_part%%\{*}
                         if [[ $port_part =~ ^[0-9]{1,5}$ ]]; then is_https_port=$port_part; fi
                         break
                         ;;
                 esac
             done <"$caddy_conf/$host.conf"
             is_tmp_https_port=$is_https_port
         fi
         if [[ -r $caddyfile ]]; then
             global_port=$(sed -nE 's/^[[:space:]]*https_port[[:space:]]+([0-9]+).*/\1/p' "$caddyfile" | head -n 1)
             [[ $global_port =~ ^[0-9]{1,5}$ && ! -f $caddy_conf/$host.conf ]] && is_https_port=$global_port
         fi

         info
         [[ $is_url ]] || exit 0 # Direct configs intentionally have no shareable URI.
         jq -cn --arg filename "$filename" --arg url "$is_url" '{filename:$filename,url:$url}'
     ) ) || exit 1
     [[ $record ]] && records+=("$record")
 done

 printf '['
 separator=
 for record in "${records[@]}"; do
     printf '%s%s' "$separator" "$record"
     separator=,
 done
 printf ']\n'

