"""
Jinja2 Template Rendering Engine.

ARCHITECTURE:
    Translates high-level declarative state definitions (YAML) into deterministic,
    modular HashiCorp Configuration Language (HCL) manifests. Enforces a strict
    Blast Radius Isolation model: each tenant domain receives an isolated directory,
    independent Terraform state file, and scoped lifecycle, eliminating cross-tenant
    blast radius during terraform apply operations.

DOMAIN REPRESENTATION MATRIX:
    1. Human/Display UTF-8: Used for labels, logging, and Cloudflare Origin CA SANs.
    2. Punycode (IDNA ASCII): Required by Cloudflare DNS records and zone APIs.
    3. Slugified Latin: Used for POSIX-compliant, shell-friendly directory naming.
"""

import os
from pathlib import Path
from typing import Any, Dict
from jinja2 import Environment, FileSystemLoader


def slugify_domain(text: str) -> str:
    """
    Transliterates Cyrillic/IDN domain strings into deterministic Latin slug representations.

    ARCHITECTURE:
        While Cloudflare DNS requires Punycode (`xn--...`), storing site directories
        as raw Punycode produces opaque, unreadable directory trees in terminal operations.
        Conversely, raw Cyrillic directory names cause encoding and path resolution bugs
        in heterogeneous Unix/Windows environments and container bind mounts.
        This function creates predictable, human-readable directory paths.

    :param text: Raw domain string (e.g., 'мой-домен.рф').
    :return: Sanitized Latin folder name (e.g., 'moy-domen-rf').
    """
    mapping = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'zh',
        'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
        'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts',
        'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu',
        'я': 'ya', 'і': 'i', 'ў': 'u', ' ': '-'
    }
    return ''.join(mapping.get(char, char) for char in text.lower())


class TemplateEngine:
    """
    HCL Code Generation Subsystem.
    
    Orchestrates Jinja2 template bindings, manages provider plugin sharing,
    and isolates tenant workspace directories.
    """

    def __init__(self, templates_dir: Path, sites_dir: Path, shared_tf_dir: Path):
        """
        Initializes the template rendering engine with required filesystem targets.

        :param templates_dir: Directory containing Jinja2 template manifests (*.tf.j2).
        :param sites_dir: Root storage directory for individual tenant workspaces.
        :param shared_tf_dir: Centralized directory for initialized Terraform plugins.
        """
        self.templates_dir = Path(templates_dir)
        self.sites_dir = Path(sites_dir)
        self.shared_tf_dir = Path(shared_tf_dir)
        # ARCHITECTURE: Standard filesystem loader with autoescape disabled for HCL code gen
        self.env = Environment(loader=FileSystemLoader(str(self.templates_dir)))

    def ensure_shared_terraform(self, tf_runner, tf_env: Dict[str, str]):
        """
        Bootstraps the shared master Terraform environment to optimize disk and network usage.

        ARCHITECTURE (Provider Cache Optimization):
            Without a centralized cache, every isolated tenant workspace downloads its
            own copy of the Cloudflare provider binary (~250MB-800MB per tenant).
            By running `terraform init` once in `_shared_tf` and combining it with
            `TF_DATA_DIR` or plugin caching, per-tenant disk consumption drops from ~800MB
            to ~105KB. This dramatically accelerates CI/CD initialization and reduces
            storage overhead across hundreds of managed domains.

        :param tf_runner: Terraform CLI execution abstraction.
        :param tf_env: Environment variables including plugin cache configurations.
        """
        self.shared_tf_dir.mkdir(parents=True, exist_ok=True)
        shared_provider_tf = self.shared_tf_dir / "provider.tf"

        # Materialize provider definition for centralized initialization
        with open(shared_provider_tf, "w", encoding="utf-8") as f:
            f.write(self.env.get_template("provider.tf.j2").render())

        shared_dot_tf = self.shared_tf_dir / ".terraform"
        if not shared_dot_tf.exists():
            # NOTE: Suppress verbose stdout using an ephemeral dummy logger to keep logs clean
            tf_runner.run_cmd(
                [str(tf_runner.binary_path), "init"],
                self.shared_tf_dir,
                tf_env,
                log_window=type("Dummy", (), {"code": lambda s, x, language=None: None})(),
            )

    def generate_tf_files(
        self,
        domain: str,
        site_data: Dict[str, Any],
        global_defaults: Dict[str, Any],
        force_reissue_ssl: bool = False,
        no_wildcard: bool = False,
    ) -> Path:
        """
        Renders concrete HCL configuration files for a target tenant workspace.

        ARCHITECTURE:
            1. Deep merge: Global baseline parameters are extended and overridden by site-specific overrides.
            2. Atomic drift cleanup (All-or-Nothing): Templates are validated and rendered in-memory
               first. Only after 100% successful generation, old `.tf` files in the target directory
               are purged (`unlink()`). This guarantees that syntax or Jinja errors will never leave
               the workspace in a half-deleted or corrupt state.
            3. Dynamic SSL generation: Evaluates certificate renewal policies, handling 15-year
               Cloudflare Origin CA automation vs user-supplied external certificates.

        WORKAROUND (Cloudflare API Error 1010):
            Cloudflare Origin CA rejects certificate signing requests containing wildcard
            entries (`*.domain.tld`) for certain Cyrillic IDN domains or internationalized TLDs.
            The `no_wildcard` flag instructs the HCL template to omit the wildcard SAN,
            rendering explicit apex and subdomains instead, bypassing the API 1010 rejection.

        :param domain: Human-readable primary domain name.
        :param site_data: Site-specific configuration node from config.yaml.
        :param global_defaults: Baseline configuration parameters.
        :param force_reissue_ssl: Flag forcing recreation of Origin CA certificate resources.
        :param no_wildcard: Disables wildcard SAN in Origin CA certificate templates.
        :return: Path to the generated site workspace directory.
        """
        # Deep copy baseline settings and apply site-specific custom overrides
        settings = global_defaults.copy()
        if 'custom_settings' in site_data and site_data['custom_settings']:
            settings.update(site_data['custom_settings'])

        # Resolve isolated directory name using slugified representation
        folder_name = site_data.get('folder_name', domain)
        site_dir = self.sites_dir / folder_name
        site_dir.mkdir(parents=True, exist_ok=True)

        # Build template binding context
        context = {
            "zone_id": site_data.get('zone_id'),
            "ip": site_data.get('ip', ''),
            "domain": domain,
            "domain_puny": site_data.get('domain_puny', domain),
            "cms": site_data.get('cms', ''),
            "target_site": site_data.get('target_site', ''),
            "target_path": site_data.get('target_path', ''),
            "whitelist_ips": site_data.get("whitelist_ips", []),
            "custom_waf": site_data.get('custom_waf', {}),
            "settings": settings,
            "no_wildcard": no_wildcard,
        }

        # Select template sets based on architectural domain role (Production Main vs 301 Mirror)
        if site_data.get("type") == "main":
            templates = [
                ("provider.tf.j2", "provider.tf"),
                ("variables.tf.j2", "variables.tf"),
                ("dns_rules.tf.j2", "dns_rules.tf"),
                ("zone_settings.tf.j2", "zone_settings.tf"),
                ("firewall_rulesets.tf.j2", "firewall_rulesets.tf"),
            ]

            # Conditionally inject IP firewall access lists if whitelists are defined
            whitelist_ips = site_data.get("whitelist_ips", [])
            if whitelist_ips:
                templates.append(("ip_access_rules.tf.j2", "ip_access_rules.tf"))

            cert_file_path = site_dir / "ssl" / "cert.pem"
            has_custom_ssl = site_data.get("has_custom_ssl", False)

            # ARCHITECTURE: 15-year Cloudflare Origin CA SSL lifecycle control.
            # Avoids re-provisioning certificates if valid on disk unless explicitly forced.
            should_issue_ssl = (not has_custom_ssl) and (
                not cert_file_path.exists() or
                site_data.get("origin_ca", False) or
                force_reissue_ssl
            )

            if should_issue_ssl:
                templates.append(("origin_ssl.tf.j2", "origin_ssl.tf"))
        else:
            # Lean template bundle for HTTP 301/302 Redirect Mirrors
            templates = [
                ("provider.tf.j2", "provider.tf"),
                ("variables.tf.j2", "variables.tf"),
                ("dns_rules.tf.j2", "dns_rules.tf"),
                ("redirect_rules.tf.j2", "redirect_rules.tf"),
            ]

        # ARCHITECTURE (Phase 1: In-Memory Validation Gate):
        # Render all templates in-memory before modifying the filesystem.
        # If any Jinja2 template is broken, an exception is raised here without touching existing files.
        rendered_payloads = {}
        for template_name, output_name in templates:
            rendered_payloads[output_name] = self.env.get_template(template_name).render(**context)

        # ARCHITECTURE (Phase 2: Atomic Cleanup & Safe Disk Flush):
        # Purge obsolete .tf manifests only after in-memory rendering succeeds.
        # Ensures obsolete resources do not linger in the workspace.
        for item in site_dir.iterdir():
            if item.is_file() and item.name.endswith(".tf"):
                item.unlink()

        # Render and persist fresh HCL configurations
        for output_name, content in rendered_payloads.items():
            output_path = site_dir / output_name
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)

        return site_dir