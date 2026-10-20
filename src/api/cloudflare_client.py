"""
Cloudflare REST API v4 Client.

ARCHITECTURE:
    Encapsulates low-level HTTP interactions with Cloudflare API v4. In this
    Infrastructure-as-Data (IaD) model, direct API calls complement Terraform
    by executing pre-flight account discovery, dynamic state reconciliation,
    and runtime edge-operations (such as zero-downtime cache purges) without
    introducing the overhead of full Terraform plan/apply lifecycles.
"""

from typing import Any, Dict, List, Optional, Tuple
import requests


class CloudflareClient:
    """
    Thin, resilient wrapper for Cloudflare REST API v4.
    
    Optimized for dynamic tenant bootstrapping, edge reconciliation,
    and fast-fail health checks before delegating state to Terraform.
    """

    # ARCHITECTURE: Root endpoint pinned to v4 to ensure stable payload contracts.
    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token: str):
        """
        Initializes the Cloudflare API client with Bearer authentication.

        :param api_token: Scoped Cloudflare API Token (not legacy Global API Key).
        :raises ValueError: If the token is empty or contains solely whitespace.
        """
        if not api_token:
            raise ValueError("Cloudflare API token must not be empty.")
        
        # HACK: Strip accidental newlines/spaces frequently introduced via
        # copy-pasting API tokens into UI inputs or environment variables,
        # preventing subtle HTTP 400/403 authorization rejections.
        self.api_token = api_token.strip()
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def get_account_id(self) -> Optional[str]:
        """
        Dynamically discovers the primary Cloudflare Account ID associated with the token.

        ARCHITECTURE:
            Avoids hardcoding account IDs across multi-tenant environments. By resolving
            the primary account context at runtime, configurations remain portable
            across different Cloudflare organizations. Requires 'Account -> Account Settings -> Read'
            permission on the provisioned token.

        :return: Account identifier string if found, otherwise None.
        """
        url = f"{self.BASE_URL}/accounts"
        try:
            # NOTE: Keep network timeout conservative (10s) to prevent pipeline stalls
            # in interactive Streamlit sessions or automated CI/CD runners.
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    accounts = data.get("result", [])
                    if accounts:
                        # Fallback to the first available organization context bound to the token
                        return accounts[0]["id"]
        except Exception:
            # Fail silently to allow callers to handle downstream fallbacks or state degradation
            pass
        return None

    def create_zone(self, domain_puny: str, account_id: str) -> Tuple[bool, Optional[str], Optional[List[str]], str]:
        """
        Provisions a new authoritative DNS zone within the target account.

        ARCHITECTURE:
            Enforces ASCII Punycode (IDNA) for the zone name parameter to prevent
            API validation errors when provisioning Internationalized Domain Names (IDNs).
            Explicitly sets `jump_start: False` to suppress automatic legacy DNS record
            scraping, guaranteeing a pristine state that won't conflict with incoming
            Terraform managed resources.

        :param domain_puny: Punycode-encoded domain name (e.g., 'xn--e1afmkfd.xn--p1ai').
        :param account_id: Destination Cloudflare account identifier.
        :return: Tuple containing (success_flag, zone_id, assigned_nameservers, error_message).
        """
        url = f"{self.BASE_URL}/zones"
        payload = {
            "name": domain_puny,
            "account": {"id": account_id},
            # NOTE: Disable record auto-import to maintain full GitOps/IaD determinism
            "jump_start": False,
            "type": "full",
        }
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=15)
            data = response.json()
            if response.status_code == 200 and data.get("success"):
                zone_id = data["result"]["id"]
                name_servers = data["result"].get("name_servers", [])
                return True, zone_id, name_servers, "Success"
            else:
                # Safely extract structured Cloudflare error messages for upstream logging
                errors = data.get("errors", [{"message": "Unknown error"}])
                error_msg = errors[0].get("message", "API request rejected")
                return False, None, None, error_msg
        except Exception as e:
            return False, None, None, str(e)

    def get_zone_rulesets(self, zone_id: str) -> List[Dict[str, Any]]:
        """
        Fetches all active rulesets bound to the specified zone.

        WORKAROUND (Cloudflare API Error 20217):
            Cloudflare automatically provisions default entrypoint rulesets for specific
            phases (such as 'http_request_firewall_custom') upon zone activation.
            When Terraform attempts to declare a 'cloudflare_ruleset' on these phases,
            Cloudflare rejects it with 'Error 20217: A ruleset already exists for this phase'.
            This method is used by the orchestrator to detect existing ruleset IDs and trigger
            an automated 'terraform import' before executing changes, ensuring self-healing state reconciliation.

        :param zone_id: Unique Cloudflare Zone identifier.
        :return: List of ruleset dictionaries describing phase, kind, and ruleset ID.
        """
        url = f"{self.BASE_URL}/zones/{zone_id}/rulesets"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            data = response.json()
            if data.get("success"):
                return data.get("result", [])
        except Exception:
            pass
        return []

    def purge_cache(self, zone_id: str, purge_everything: bool = True) -> Tuple[bool, str]:
        """
        Flushes Cloudflare Edge CDN caches for the target zone.

        ARCHITECTURE:
            Executing cache purges via direct REST calls rather than Terraform state
            preserves IaD idempotency; cache purges are ephemeral operational triggers,
            not persistent state declarations.

        :param zone_id: Unique Cloudflare Zone identifier.
        :param purge_everything: Invalidate all cached assets across all edge PoPs.
        :return: Tuple containing (success_flag, error_message).
        """
        url = f"{self.BASE_URL}/zones/{zone_id}/purge_cache"
        payload = {"purge_everything": purge_everything}
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=15)
            data = response.json()
            if data.get("success"):
                return True, ""
            errors = data.get("errors") or [{}]
            err = errors[0].get("message", "API Error")
            return False, err
        except Exception as e:
            return False, str(e)
            
    def delete_zone(self, zone_id: str) -> Tuple[bool, str]:
        """
        Deletes a DNS zone completely from the Cloudflare account.

        ARCHITECTURE:
            Used during tenant decommissioning. Permanently purges the zone
            and all associated edge configurations from Cloudflare Edge.

        :param zone_id: Unique Cloudflare Zone identifier.
        :return: Tuple containing (success_flag, error_message).
        """
        url = f"{self.BASE_URL}/zones/{zone_id}"
        try:
            response = requests.delete(url, headers=self.headers, timeout=15)
            data = response.json()
            if response.status_code == 200 and data.get("success"):
                return True, ""
            errors = data.get("errors", [{"message": "Unknown error"}])
            return False, errors[0].get("message", "Failed to delete zone")
        except Exception as e:
            return False, str(e)