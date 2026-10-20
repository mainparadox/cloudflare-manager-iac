"""
Terraform Execution & Self-Healing Orchestration Service.

ARCHITECTURE:
    Implements an autonomous Infrastructure-as-Data (IaD) reconciliation loop.
    Rather than treating Terraform as a passive CLI tool, this service acts as an
    intelligent control plane wrapper. It intercepts domain-specific Cloudflare API
    edge errors (20217, 1010, 1014) in real-time, executes corrective out-of-band state
    remediation (dynamic imports, schema regeneration, zombie state purging), and
    re-converges the infrastructure towards the desired declarative state.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from src.api.cloudflare_client import CloudflareClient
from src.core.i18n import I18n


class TerraformRunner:
    """
    Orchestrates Terraform CLI operations with integrated self-healing capabilities,
    real-time streaming telemetry, and provider cache deduplication.
    """

    def __init__(
        self,
        binary_path: Path,
        base_dir: Path,
        shared_tf_dir: Path,
        plugin_cache_dir: Path,
        i18n: I18n,
    ):
        """
        Initializes the execution service with runtime dependencies and isolation paths.

        :param binary_path: Path to the terraform executable binary.
        :param base_dir: Root workspace directory for the project.
        :param shared_tf_dir: Centralized directory hosting initialized provider plugins.
        :param plugin_cache_dir: Global shared cache directory for downloaded provider binaries.
        :param i18n: Localization engine instance for telemetry logging.
        """
        self.binary_path = Path(binary_path)
        self.base_dir = Path(base_dir)
        self.shared_tf_dir = Path(shared_tf_dir)
        self.plugin_cache_dir = Path(plugin_cache_dir)
        self.i18n = i18n

    def build_env(self, api_token: str, use_mirror: bool = False, cli_config_path: Optional[Path] = None) -> Dict[str, str]:
        """
        Constructs an isolated execution environment for child Terraform sub-processes.

        ARCHITECTURE (Provider Cache Optimization):
            By pinning `TF_DATA_DIR` to `shared_tf_dir / ".terraform"`, all tenant
            workspaces reuse the pre-initialized Cloudflare provider metadata and plugin
            binaries. This deduplicates provider downloads across all managed domains,
            slashing disk consumption from ~800MB per tenant to approximately ~105KB
            (consisting solely of the HCL manifests and `.tfstate`).

        :param api_token: Scoped Cloudflare API authentication token.
        :param use_mirror: Flag enabling internal network mirror for provider downloads.
        :param cli_config_path: Path to custom .terraformrc / terraform.rc configuration.
        :return: Sanitized environment variable mapping for process execution.
        """
        tf_env = os.environ.copy()
        # ARCHITECTURE: Direct global provider caching across independent workspaces
        tf_env["TF_PLUGIN_CACHE_DIR"] = str(self.plugin_cache_dir)
        # NOTE: Informs Terraform CLI it is running unattended; strips interactive prompts and ANSI decorations
        tf_env["TF_IN_AUTOMATION"] = "true"
        # Shared provider memory footprint optimization
        tf_env["TF_DATA_DIR"] = str(self.shared_tf_dir / ".terraform")
        tf_env["CLOUDFLARE_API_TOKEN"] = api_token

        # WORKAROUND: Support internal registry mirrors when executing in restricted network topologies
        if use_mirror and cli_config_path and cli_config_path.exists():
            tf_env["TF_CLI_CONFIG_FILE"] = str(cli_config_path)
        elif "TF_CLI_CONFIG_FILE" in tf_env:
            del tf_env["TF_CLI_CONFIG_FILE"]

        return tf_env

    def run_cmd(
        self,
        command: List[str],
        cwd: Path,
        env_vars: Dict[str, str],
        log_window: Any,
        full_log: str = "",
    ) -> Tuple[int, str]:
        """
        Executes a subprocess command with non-blocking, batched real-time log streaming.

        PERFORMANCE OPTIMIZATION (UI Batch Buffering):
            Emitting every stdout line directly to a web interface (e.g., Streamlit)
            triggers severe event-loop contention and WebSocket frame saturation.
            This method implements a hybrid debouncing strategy: flushes buffers
            every 5 lines OR every 500 milliseconds, guaranteeing high-fps smooth
            rendering while maintaining responsive UI execution.

        :param command: CLI arguments tokenized as a list of strings.
        :param cwd: Working directory for execution context.
        :param env_vars: Scoped process environment variables.
        :param log_window: Reactive UI component exposing `.code()` log sink.
        :param full_log: Pre-existing log accumulator string.
        :return: Tuple containing (process_return_code, aggregated_log_output).
        """
        step_title = self.i18n.t("runner.executing").format(cmd=" ".join(command))
        full_log += f"\n{step_title}\n"
        log_window.code(full_log, language="bash")

        try:
            # NOTE: bufsize=1 enables line-buffered read access for real-time capture
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env_vars,
            )
        except FileNotFoundError:
            error_msg = "\n" + self.i18n.t("runner.binary_not_found").format(
                cmd=command[0], path=self.binary_path
            ) + "\n"
            full_log += error_msg
            log_window.code(full_log, language="bash")
            return 1, full_log

        lines_buffer: List[str] = []
        last_update_time = time.time()

        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                lines_buffer.append(line)
                # Flush buffer if line threshold met or time debounce interval elapsed
                if len(lines_buffer) >= 5 or (time.time() - last_update_time > 0.5):
                    full_log += "".join(lines_buffer)
                    log_window.code(full_log, language="bash")
                    lines_buffer = []
                    last_update_time = time.time()

        # Flush any remaining un-buffered output lines post-loop
        if lines_buffer:
            full_log += "".join(lines_buffer)
            log_window.code(full_log, language="bash")

        process.wait()
        return process.returncode, full_log
    
    def destroy_site(
        self,
        site_dir: Path,
        tf_env: Dict[str, str],
        log_window: Any,
    ) -> Tuple[bool, str]:
        """
        Executes declarative teardown of all managed tenant infrastructure.

        ARCHITECTURE:
            Invokes `terraform destroy` to cleanly wipe DNS records, WAF rulesets,
            and Origin CA certificates from remote provider state before local
            workspace directory eviction.

        :param site_dir: Target directory holding tenant .tf manifests and state.
        :param tf_env: Execution environment variables.
        :param log_window: Reactive UI log sink.
        :return: Tuple containing (success_boolean, aggregated_log).
        """
        full_log = ""
        tf_exe_str = str(self.binary_path)

        if not site_dir.exists():
            return True, "Workspace directory does not exist, skipping destroy."

        destroy_cmd = [tf_exe_str, "destroy", "-auto-approve"]
        code, full_log = self.run_cmd(destroy_cmd, site_dir, tf_env, log_window, full_log)
        return (code == 0), full_log

    def apply_site_with_healing(
        self,
        domain: str,
        site_data: Dict[str, Any],
        site_dir: Path,
        tf_env: Dict[str, str],
        cf_client: CloudflareClient,
        log_window: Any,
        generate_callback: Callable[..., Path],
        global_defaults: Dict[str, Any],
        force_reissue_ssl: bool = False,
    ) -> Tuple[bool, str]:
        """
        Executes Terraform provisioning with a multi-phase Self-Healing state reconciliation engine.

        AUTOMATED RECOVERY CIRCUITS:
            1. Cloudflare API Error 20217 (Hidden Ruleset Collision):
               Cloudflare implicitly provisions zone-level ruleset entrypoints upon zone setup.
               Subsequent Terraform creation throws 20217. The engine queries the REST API,
               maps phase collision targets, and executes targeted `terraform import` operations.
            2. Cloudflare API Error 1010 (IDN Wildcard Rejection):
               Origin CA fails to validate wildcard certs (*.domain) for specific IDN/Cyrillic zones.
               The engine dynamically alters template parameters (`no_wildcard=True`), regenerates
               the HCL manifests, and re-executes provisioning.
            3. Cloudflare API Error 1014 (Out-of-band Revocation Drift):
               If a certificate was revoked via the Cloudflare web UI, Terraform state holds a dead
               resource ID causing 1014 during refresh. The engine invokes `terraform state rm`
               to clear zombie state and issues a fresh certificate.

        :param domain: Primary domain string under reconciliation.
        :param site_data: Site specification dictionary.
        :param site_dir: Target directory holding tenant .tf manifests and state.
        :param tf_env: Execution environment variables with credentials and plugin paths.
        :param cf_client: Active Cloudflare REST API client for discovery.
        :param log_window: Reactive UI log sink.
        :param generate_callback: Callback to re-render HCL templates on schema fallback.
        :param global_defaults: Baseline infrastructure parameters.
        :param force_reissue_ssl: Enforces cryptographic key regeneration and certificate taint.
        :return: Tuple containing (success_boolean, comprehensive_execution_log).
        """
        full_log = ""
        tf_exe_str = str(self.binary_path)

        # 1. Terraform Init
        init_code, full_log = self.run_cmd([tf_exe_str, "init"], site_dir, tf_env, log_window, full_log)
        if init_code != 0:
            return False, full_log

        # 2. Build Apply Command
        apply_cmd = [tf_exe_str, "apply", "-auto-approve"]
        if force_reissue_ssl:
            # NOTE: Explicit resource taint/replacement pattern to re-generate TLS keypairs and SAN certs
            apply_cmd.extend([
                "-replace=cloudflare_origin_ca_certificate.origin_cert",
                "-replace=tls_private_key.origin_key",
            ])

        # 3. First Apply Run
        apply_code, full_log = self.run_cmd(apply_cmd, site_dir, tf_env, log_window, full_log)

        # =========================================================================
        # SELF-HEALING 1: ERROR 20217 (Rulesets Conflict)
        # =========================================================================
        if apply_code != 0 and "20217" in full_log:
            conflict_msg = f"\n[SELF-HEALING] {self.i18n.t('healing.ruleset_conflict')}\n"
            full_log += conflict_msg
            log_window.code(full_log, language="bash")

            rulesets = cf_client.get_zone_rulesets(site_data.get("zone_id", ""))
            
            # Map Cloudflare phase names to their declarative Terraform resource addresses
            phase_to_resource = {
                "http_request_dynamic_redirect": "cloudflare_ruleset.domain_redirect",
                "http_request_firewall_custom": "cloudflare_ruleset.custom_waf_rules",
                "http_ratelimit": "cloudflare_ruleset.rate_limit_rules",
            }

            for rs in rulesets:
                phase = rs.get("phase")
                rs_id = rs.get("id")
                tf_resource = phase_to_resource.get(phase)

                # WORKAROUND: If the phase matches the failed apply context, reconcile state via import
                if tf_resource and phase in full_log:
                    import_target = f"zones/{site_data.get('zone_id')}/{rs_id}"
                    msg = self.i18n.t("healing.importing").format(resource=tf_resource, target=import_target)
                    full_log += f"\n{msg}\n"
                    log_window.code(full_log, language="bash")
                    
                    _, full_log = self.run_cmd([tf_exe_str, "import", tf_resource, import_target], site_dir, tf_env, log_window, full_log)

            # Retry Apply post-import
            full_log += f"\n{self.i18n.t('healing.retry_apply')}\n"
            apply_code, full_log = self.run_cmd(apply_cmd, site_dir, tf_env, log_window, full_log)

        # =========================================================================
        # SELF-HEALING 2: ERROR 1010 (IDN Wildcard Validation failure)
        # =========================================================================
        if apply_code != 0 and "1010" in full_log:
            wildcard_msg = f"\n[SELF-HEALING] {self.i18n.t('healing.wildcard_rejected').format(domain=domain)}\n"
            full_log += wildcard_msg
            log_window.code(full_log, language="bash")
            
            # WORKAROUND: Fall back to non-wildcard SAN certificate by triggering template regeneration
            generate_callback(domain, site_data, global_defaults, force_reissue_ssl=force_reissue_ssl, no_wildcard=True)
            
            # Retry Apply without wildcard
            full_log += f"\n{self.i18n.t('healing.retry_apply')}\n"
            apply_code, full_log = self.run_cmd(apply_cmd, site_dir, tf_env, log_window, full_log)

        # =========================================================================
        # SELF-HEALING 3: ERROR 1014 (Certificate Already Revoked / Deleted manually)
        # =========================================================================
        if apply_code != 0 and "1014" in full_log:
            revocation_msg = f"\n[SELF-HEALING] {self.i18n.t('healing.cert_revoked')}\n"
            full_log += revocation_msg
            log_window.code(full_log, language="bash")

            # WORKAROUND: Purge zombie state reference; allows Terraform to generate a new cert on retry
            _, full_log = self.run_cmd([tf_exe_str, "state", "rm", "cloudflare_origin_ca_certificate.origin_cert"], site_dir, tf_env, log_window, full_log)
            
            # Retry Apply to issue fresh certificate
            full_log += f"\n{self.i18n.t('healing.retry_apply')}\n"
            apply_code, full_log = self.run_cmd(apply_cmd, site_dir, tf_env, log_window, full_log)

        return (apply_code == 0), full_log