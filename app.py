"""
Cloudflare IaC Management Portal - Main Application.

ARCHITECTURE:
    Serves as the interactive control plane orchestrator for this Infrastructure-as-Data
    (IaD) platform. Integrates Streamlit (reactive web UI), Terraform v5.x (declarative
    engine), Jinja2 (HCL codegen), and the Cloudflare REST API v4 (telemetry & healing).
    
SEPARATION OF CONCERNS:
    - Pure ID-Driven Architecture: Zero hardcoded human-readable strings exist in source.
      All labels, descriptions, and notifications are resolved via the I18n subsystem.
    - SSOT Alignment: UI mutations update in-memory state, write back to `config.yaml`,
      and trigger idempotent, scoped Terraform runs with isolated blast radiuses.
"""

import os
from pathlib import Path
import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from src.api.cloudflare_client import CloudflareClient
from src.core.config_manager import ConfigManager
from src.core.i18n import I18n
from src.services.template_engine import TemplateEngine, slugify_domain
from src.services.terraform_runner import TerraformRunner

# Load local environment secrets (.env) for development portability
load_dotenv()

# ARCHITECTURE: Base path definitions anchoring workspace isolation and shared caches
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
SITES_DIR = BASE_DIR / "sites"
SHARED_TF_DIR = BASE_DIR / "_shared_tf"
TEMPLATES_DIR = BASE_DIR / "_templates"
PLUGIN_CACHE_DIR = BASE_DIR / "tf_plugin_cache"
TF_EXE = BASE_DIR / "terraform.exe"

# WORKAROUND: Robust discovery of locales folder supporting both root and src/ layouts
LOCALES_DIR = BASE_DIR / "src" / "locales"
if not LOCALES_DIR.exists():
    LOCALES_DIR = BASE_DIR / "locales"

# Configure root Streamlit viewport
st.set_page_config(page_title="Cloudflare IaC Portal", layout="wide", page_icon="☁️")

# WORKAROUND: Intercept solitary 'C' keydown and auto-dismiss Streamlit's cache modal
components.html(
    """
<script>
(function() {
    const pDoc = window.parent.document;
    const pWin = window.parent;

    // 1. Intercept 'c' key in capture phase on both document and window
    function blockKey(e) {
        if (e.key && e.key.toLowerCase() === 'c' && !e.ctrlKey && !e.metaKey && !e.altKey) {
            const el = pDoc.activeElement;
            const tag = el ? el.tagName : '';
            if (tag !== 'INPUT' && tag !== 'TEXTAREA' && !el.isContentEditable) {
                e.stopImmediatePropagation();
                e.stopPropagation();
                e.preventDefault();
            }
        }
    }
    pDoc.addEventListener('keydown', blockKey, true);
    pWin.addEventListener('keydown', blockKey, true);

    // 2. Safety Net: Instantly auto-dismiss modal if triggered
    const observer = new MutationObserver(() => {
        const modals = pDoc.querySelectorAll('[role="dialog"], [data-testid="stModal"]');
        modals.forEach(modal => {
            if (modal.innerText && modal.innerText.includes('Clear caches')) {
                const cancelBtn = Array.from(modal.querySelectorAll('button')).find(
                    b => b.innerText.trim().toLowerCase() === 'cancel'
                );
                if (cancelBtn) cancelBtn.click();
            }
        });
    });
    observer.observe(pDoc.body, { childList: true, subtree: true });
})();
</script>
""",
    height=1,
)


def detect_browser_language(default_lang: str = "ru") -> str:
    """
    Inspects incoming HTTP Accept-Language headers to negotiate user locale.

    ARCHITECTURE:
        Extracts client headers injected by reverse proxies or browser contexts.
        Enables seamless zero-click onboarding into the engineer's native language.

    :param default_lang: Fallback ISO language identifier if negotiation yields no match.
    :return: Resolved language code ('ru' or 'en').
    """
    try:
        if hasattr(st, "context") and hasattr(st.context, "headers"):
            accept_lang = st.context.headers.get("Accept-Language", "").lower()
            if any(lang in accept_lang for lang in ["ru", "be"]):
                return "ru"
            if "en" in accept_lang:
                return "en"
    except Exception:
        # Silently degrade to default locale on headless runs or mock contexts
        pass
    return default_lang


# Initialize session state locale with browser auto-detection
if "locale" not in st.session_state:
    st.session_state.locale = detect_browser_language(default_lang="ru")

# Bootstrap foundational core engine singletons
i18n = I18n(LOCALES_DIR, default_locale=st.session_state.locale)
config_mgr = ConfigManager(CONFIG_FILE)
template_engine = TemplateEngine(TEMPLATES_DIR, SITES_DIR, SHARED_TF_DIR)
tf_runner = TerraformRunner(TF_EXE, BASE_DIR, SHARED_TF_DIR, PLUGIN_CACHE_DIR, i18n)

# API client initialization; supports explicit account targeting via .env
api_token = os.getenv("CLOUDFLARE_API_TOKEN", "")
account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
cf_client = CloudflareClient(api_token, account_id=account_id) if api_token else None


def get_safe_index(options_list, value, default_val):
    """
    Safely resolves selectbox array indices, mitigating ValueError crashes on schema drift.

    :param options_list: Target list of available choices.
    :param value: Candidate selection value.
    :param default_val: Fallback option if value is not present in list.
    :return: Zero-based integer index.
    """
    if value in options_list:
        return options_list.index(value)
    return options_list.index(default_val) if default_val in options_list else 0


@st.cache_data(ttl=60)
def check_hashicorp_access():
    """
    Probes connectivity to HashiCorp's official registry endpoint.

    ARCHITECTURE:
        Used for proactive network diagnostic checks. Engineers operating under
        geo-blocks, strict corporate egress firewalls, or regional ISP filtering
        are alerted immediately, triggering the automated Terraform mirror subsystem.

    :return: True if registry is reachable via low-latency HEAD request, else False.
    """
    try:
        res = requests.head("https://registry.terraform.io", timeout=1.5)
        return res.status_code < 400
    except requests.RequestException:
        return False


# State initialization: Ensure single source of truth is loaded in session state
if "config" not in st.session_state:
    st.session_state.config = config_mgr.load_config()
if "saved_redirect_state" not in st.session_state:
    st.session_state.saved_redirect_state = False

config = st.session_state.config
sites = config.get("sites", {})
global_defaults = config.get("global_defaults", {})


# --- SIDEBAR (PURE ID-DRIVEN) ---
st.sidebar.title("☁️ CF Manager")

# Language switcher with instant reactive rerun trigger
lang_options = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English"}
chosen_lang = st.sidebar.selectbox(
    i18n.t("nav.language"),
    options=list(lang_options.keys()),
    format_func=lambda x: lang_options[x],
    index=0 if st.session_state.locale == "ru" else 1,
)

if chosen_lang != st.session_state.locale:
    st.session_state.locale = chosen_lang
    i18n.set_locale(chosen_lang)
    st.rerun()

# Dynamic navigation mapped to dot-notation i18n keys
menu_ids = ["dashboard", "add_domain", "zone_settings", "waf", "tools"]
active_tab = st.sidebar.radio(
    i18n.t("nav.navigation"),
    options=menu_ids,
    format_func=lambda k: i18n.t(f"menu.{k}"),
)


# ==============================================================================
# 1. TAB: DASHBOARD
# ==============================================================================
if active_tab == "dashboard":
    st.header(i18n.t("dashboard.title"))
    main_sites = {k: v for k, v in sites.items() if v.get("type") == "main"}

    # Render primary infrastructure cards with hierarchical topology visualization
    for main_domain, main_data in main_sites.items():
        status_icon = "🟢" if main_data.get("active") else "🔴"
        folder_name = main_data.get("folder_name", main_domain)
        cert_path = SITES_DIR / folder_name / "ssl" / "cert.pem"

        # NOTE: Real-time certificate presence audit directly from local workspace state
        if main_data.get("has_custom_ssl"):
            ssl_badge = f"`{i18n.t('dashboard.custom_ssl')}`"
        elif cert_path.exists():
            ssl_badge = f"`{i18n.t('dashboard.origin_ssl')}`"
        else:
            ssl_badge = f"`{i18n.t('dashboard.no_ssl')}`"

        with st.container(border=True):
            cms_title = main_data.get('cms', 'unknown').upper()
            st.markdown(f"### {status_icon} 🏢 **{main_domain}** &nbsp; `[{cms_title}]` &nbsp; {ssl_badge}")
            
            ip_lbl = i18n.t("dashboard.ip_label")
            zone_lbl = i18n.t("dashboard.zone_label")
            folder_lbl = i18n.t("dashboard.folder_label")
            st.caption(f"**{ip_lbl}** `{main_data.get('ip')}` | **{zone_lbl}** `{main_data.get('zone_id')}` | **{folder_lbl}** `{folder_name}`")

            # Topology tree: Correlate and display 301/302 mirrors subordinate to this primary tenant
            mirrors = {k: v for k, v in sites.items() if v.get("type") == "redirect" and v.get("target_site") == main_domain}
            if mirrors:
                for m_domain, m_data in mirrors.items():
                    m_status = "🟢" if m_data.get("active") else "🔴"
                    t_path = m_data.get("target_path", "")
                    redir_text = i18n.t("dashboard.redirect_to")
                    st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp; └── {m_status} ↪️ **{m_domain}** ({redir_text} **{main_domain}{t_path}**)")


# ==============================================================================
# 2. TAB: ZONE SETTINGS
# ==============================================================================
elif active_tab == "zone_settings":
    st.title(i18n.t("zone_settings.title"))

    col_select, col_check = st.columns([3, 1])
    with col_check:
        st.write("")
        st.write("")

        def save_redirect_toggle():
            # State synchronization callback to preserve filter toggles across reruns
            st.session_state.saved_redirect_state = st.session_state.toggle_widget_key

        show_redirects = st.checkbox(
            i18n.t("zone_settings.only_redirects"),
            value=st.session_state.saved_redirect_state,
            key="toggle_widget_key",
            on_change=save_redirect_toggle,
        )

    # Sentinel identifiers for batch global policy management vs granular tenant overrides
    GLOBAL_MAIN = "__GLOBAL_MAIN__"
    GLOBAL_REDIRECT = "__GLOBAL_REDIRECT__"

    if show_redirects:
        target_options = [GLOBAL_REDIRECT] + [k for k, v in sites.items() if v.get("type") == "redirect"]
    else:
        target_options = [GLOBAL_MAIN] + [k for k, v in sites.items() if v.get("type") == "main"]

    def format_target(x):
        if x == GLOBAL_MAIN:
            return i18n.t("zone_settings.all_main")
        if x == GLOBAL_REDIRECT:
            return i18n.t("zone_settings.all_redirects")
        return x

    target = st.selectbox(i18n.t("zone_settings.select_target"), target_options, format_func=format_target) if target_options else None

    st.markdown("---")
    if not target:
        st.info(i18n.t("zone_settings.no_sites"))
        st.stop()

    is_global = target in [GLOBAL_MAIN, GLOBAL_REDIRECT]

    if is_global:
        site_type = "redirect" if show_redirects else "main"
        site_data = {}
    else:
        site_data = sites.get(target, {})
        site_type = site_data.get("type", "main")

    # Inherit and overlay site-specific overrides on top of baseline global parameters
    if is_global and site_type == "main":
        current_settings = global_defaults.copy()
        st.info(i18n.t("zone_settings.global_info"))
    elif not is_global:
        current_settings = global_defaults.copy()
        if "custom_settings" in site_data and site_data["custom_settings"]:
            current_settings.update(site_data["custom_settings"])

        if site_type == "main":
            warn_col, btn_col = st.columns([4, 1])
            with warn_col:
                st.warning(i18n.t("zone_settings.custom_info").format(domain=target))
            with btn_col:
                # Provide instant atomic reset of tenant overrides back to global baseline
                if st.button(i18n.t("zone_settings.reset_btn"), use_container_width=True):
                    st.session_state.config["sites"][target]["custom_settings"] = {}
                    config_mgr.save_config(st.session_state.config)
                    st.rerun()

    if site_type == "main":
        # Form encapsulation batches inputs, preventing expensive intermediate reruns
        with st.form("zone_settings_form"):
            current_ip = site_data.get("ip", "")
            new_ip = current_ip
            if not is_global:
                new_ip = st.text_input(
                    i18n.t("zone_settings.server_ip_field"),
                    value=current_ip,
                    help=i18n.t("zone_settings.server_ip_help"),
                ).strip()
                st.markdown("---")
            col1, col2 = st.columns(2)

            with col1:
                always_https = st.toggle(
                    i18n.t("zone_settings.always_https_label"), 
                    value=current_settings.get("always_https", True)
                )
                browser_check = st.toggle(
                    i18n.t("zone_settings.browser_check_label"), 
                    value=current_settings.get("browser_check", True)
                )
                email_obfus = st.toggle(
                    i18n.t("zone_settings.email_obfus_label"), 
                    value=current_settings.get("email_obfuscation", True)
                )

            with col2:
                sec_options = ["off", "essentially_off", "low", "medium", "high", "under_attack"]
                sec_val = current_settings.get("security_level", "medium")
                security_level = st.selectbox(
                    i18n.t("zone_settings.security_level_label"), 
                    options=sec_options, 
                    index=get_safe_index(sec_options, sec_val, "medium")
                )

                tls_options = ["1.0", "1.1", "1.2", "1.3"]
                tls_val = current_settings.get("min_tls_version", "1.2")
                min_tls = st.selectbox(
                    i18n.t("zone_settings.min_tls_label"), 
                    options=tls_options, 
                    index=get_safe_index(tls_options, tls_val, "1.2")
                )

                geo_keys = ["by_ru_eu", "by_ru", "by_only"]
                current_geo = current_settings.get("geo_filter", "by_ru_eu")
                geo_filter = st.selectbox(
                    i18n.t("zone_settings.geo_filter_label"),
                    options=geo_keys,
                    format_func=lambda x: i18n.t(f"zone_settings.geo_options.{x}"),
                    index=get_safe_index(geo_keys, current_geo, "by_ru_eu"),
                )

            force_geo = True
            force_all_reset = False
            if is_global:
                st.write("---")
                force_geo = st.checkbox(i18n.t("zone_settings.force_geo_checkbox"), value=True)
                force_all_reset = st.checkbox(i18n.t("zone_settings.force_all_reset_checkbox"), value=False)
                if force_all_reset:
                    st.error(i18n.t("zone_settings.force_reset_warn"))

            submitted = st.form_submit_button(i18n.t("zone_settings.save_btn"))

        # ARCHITECTURE (Reactive UI Outside st.form):
        # SSL toggle and certificate reissuance controls are deliberately placed outside
        # `st.form`. Streamlit forms block immediate UI updates until form submission.
        # Placing SSL toggles outside allows instant reactive re-rendering (e.g., dynamically
        # toggling between external cert indicators and Origin CA generation controls).
        st.markdown("---")
        st.markdown(f"#### {i18n.t('ssl_block.title')}")
        force_reissue_ssl = False

        if is_global:
            mass_issue_ssl = st.checkbox(i18n.t("ssl_block.mass_issue"), value=False)
            force_reissue_ssl = st.checkbox(i18n.t("ssl_block.force_reissue"), value=False)
            if mass_issue_ssl:
                st.info(i18n.t("ssl_block.mass_issue_info"))
        else:
            site_folder = site_data.get("folder_name", target)
            cert_file = SITES_DIR / site_folder / "ssl" / "cert.pem"
            cert_exists = cert_file.exists()

            custom_ssl_val = site_data.get("has_custom_ssl", False)
            custom_ssl_toggle = st.checkbox(i18n.t("ssl_block.custom_ssl_toggle"), value=custom_ssl_val)
            if custom_ssl_toggle != custom_ssl_val:
                # Immediate state mutation and persistence upon checkbox interaction
                st.session_state.config["sites"][target]["has_custom_ssl"] = custom_ssl_toggle
                config_mgr.save_config(st.session_state.config)
                st.rerun()

            if cert_exists:
                st.success(i18n.t("ssl_block.issued_success"))
                force_reissue_ssl = st.checkbox(i18n.t("ssl_block.reissue_single"), value=False)
            elif not custom_ssl_toggle:
                st.info(i18n.t("ssl_block.not_issued_info"))

        if submitted:
            if not is_global and not new_ip:
                st.error(i18n.t("zone_settings.err_empty_ip"))
                st.stop()
            new_settings = {
                "always_https": always_https,
                "browser_check": browser_check,
                "email_obfuscation": email_obfus,
                "security_level": security_level,
                "min_tls_version": min_tls,
                "geo_filter": geo_filter,
                "challenge_ttl": current_settings.get("challenge_ttl", 31536000),
            }

            if is_global:
                # Propagate baseline settings globally across the infrastructure state
                st.session_state.config["global_defaults"] = new_settings
                if mass_issue_ssl:
                    for s_name, s_data in st.session_state.config["sites"].items():
                        if s_data.get("type") == "main" and not s_data.get("has_custom_ssl"):
                            s_data["origin_ca"] = True

                if force_all_reset:
                    for site_name, s_data in st.session_state.config["sites"].items():
                        if "custom_settings" in s_data:
                            saved_geo = None
                            if force_geo and "geo_filter" in s_data["custom_settings"]:
                                saved_geo = s_data["custom_settings"]["geo_filter"]
                            s_data["custom_settings"] = {}
                            if saved_geo:
                                s_data["custom_settings"]["geo_filter"] = saved_geo
                elif not force_geo:
                    for site_name, s_data in st.session_state.config["sites"].items():
                        if "custom_settings" in s_data and "geo_filter" in s_data["custom_settings"]:
                            del s_data["custom_settings"]["geo_filter"]
            else:
                st.session_state.config["sites"][target]["ip"] = new_ip
                # Compute and store minimal delta overrides against global defaults
                custom = {k: v for k, v in new_settings.items() if v != global_defaults.get(k)}
                st.session_state.config["sites"][target]["custom_settings"] = custom
                st.session_state.config["sites"][target]["has_custom_ssl"] = custom_ssl_toggle
                if not custom_ssl_toggle:
                    st.session_state.config["sites"][target]["origin_ca"] = True

            config_mgr.save_config(st.session_state.config)
            st.success(i18n.t("zone_settings.saved_success"))

            # Provide granular feedback on fleet-wide override mutations
            if is_global:
                if force_all_reset:
                    if force_geo:
                        st.warning(i18n.t("zone_settings.sync_geo_preserved"))
                    else:
                        st.warning(i18n.t("zone_settings.sync_all_wiped"))
                elif not force_geo:
                    st.warning(i18n.t("zone_settings.sync_geo_reset"))
                else:
                    st.info(i18n.t("zone_settings.sync_defaults_updated"))

        with st.expander(i18n.t("zone_settings.background_settings_title")):
            st.markdown(i18n.t("zone_settings.background_settings_desc"))

        # In-browser certificate inspection for rapid operations and debugging
        if not is_global:
            site_folder = site_data.get("folder_name", target)
            cert_path = SITES_DIR / site_folder / "ssl" / "cert.pem"
            key_path = SITES_DIR / site_folder / "ssl" / "key.pem"

            if cert_path.exists() and key_path.exists():
                with st.expander(i18n.t("zone_settings.view_keys_expander")):
                    st.success(i18n.t("zone_settings.view_keys_success"))
                    with open(cert_path, "r", encoding="utf-8") as f:
                        cert_content = f.read()
                    with open(key_path, "r", encoding="utf-8") as f:
                        key_content = f.read()

                    st.markdown(f"**{i18n.t('zone_settings.cert_label')}**")
                    st.code(cert_content, language="text")
                    st.markdown(f"**{i18n.t('zone_settings.key_label')}**")
                    st.code(key_content, language="text")

    elif site_type == "redirect":
        if is_global:
            st.info(i18n.t("zone_settings.redirect_global_info"))
        else:
            t_main = site_data.get("target_site", "unknown")
            t_path = site_data.get("target_path", "")
            st.info(i18n.t("zone_settings.redirect_single_info").format(domain=target, target=t_main, path=t_path))

    # TERRAFORM RUNNER EXECUTION ORCHESTRATOR
    st.markdown("---")
    st.subheader(i18n.t("zone_settings.apply_title"))

    # Dynamic network mirror configuration to bypass geo-restrictions and ISP outages
    with st.expander(i18n.t("network.title"), expanded=True):
        hashicorp_accessible = check_hashicorp_access()

        if "mirror_checkbox_key" not in st.session_state:
            st.session_state.mirror_checkbox_key = not hashicorp_accessible

        if hashicorp_accessible:
            st.success(i18n.t("network.vpn_ok"))
        else:
            st.warning(i18n.t("network.vpn_blocked"))

        use_mirror = st.checkbox(i18n.t("network.use_mirror"), key="mirror_checkbox_key")

        selected_mirror = None
        if use_mirror:
            mirrors = {
                "Selectel (Default)": "https://tf-proxy.selectel.ru/mirror/v1/",
                "TF.org.ru": "https://nm.tf.org.ru/",
                "NationalCDN": "https://registry.nationalcdn.ru/",
                "Yandex Cloud": "https://terraform-mirror.yandexcloud.net/",
                "Comcloud": "https://registry.comcloud.xyz/",
            }
            if "mirror_selectbox_key" not in st.session_state:
                st.session_state.mirror_selectbox_key = "Selectel (Default)"

            mirror_name = st.selectbox(i18n.t("network.select_mirror"), options=list(mirrors.keys()), key="mirror_selectbox_key")
            selected_mirror = mirrors[mirror_name]
        else:
            st.info(i18n.t("network.direct_download"))

        def on_recheck_click():
            # Invalidate cached connectivity check to trigger fresh probe
            check_hashicorp_access.clear()
            is_accessible_now = check_hashicorp_access()
            st.session_state.mirror_checkbox_key = not is_accessible_now

        st.button(i18n.t("network.recheck_btn"), use_container_width=True, on_click=on_recheck_click)

    if st.button(i18n.t("zone_settings.apply_btn"), type="primary"):
        # Resolve target batch or single site
        if is_global:
            if site_type == "main":
                targets_to_apply = {k: v for k, v in sites.items() if v.get("type") == "main"}.items()
            else:
                targets_to_apply = {k: v for k, v in sites.items() if v.get("type") == "redirect"}.items()
        else:
            targets_to_apply = {target: site_data}.items()

        # ARCHITECTURE: Dynamic CLI configuration (`terraform.rc`) injection.
        # Forces provider installations through selected network mirror without mutating system configs.
        cli_config_path = BASE_DIR / "terraform.rc"
        if use_mirror and selected_mirror:
            rc_content = f"""provider_installation {{
  network_mirror {{
    url = "{selected_mirror}"
    include = ["registry.terraform.io/*/*"]
  }}
  direct {{
    exclude = ["registry.terraform.io/*/*"]
  }}
}}"""
            with open(cli_config_path, "w", encoding="utf-8") as f:
                f.write(rc_content)
        else:
            if cli_config_path.exists():
                cli_config_path.unlink()

        # Build isolated process environment with centralized provider cache
        tf_env = tf_runner.build_env(api_token, use_mirror=use_mirror, cli_config_path=cli_config_path)

        with st.spinner("Checking Terraform plugins..."):
            template_engine.ensure_shared_terraform(tf_runner, tf_env)

        # Execution pipeline: Iterate across targets maintaining isolated blast radiuses
        for domain, data in targets_to_apply:
            if data.get("active"):
                st.markdown(i18n.t("zone_settings.processing_site").format(domain=domain))
                site_dir = template_engine.generate_tf_files(domain, data, global_defaults, force_reissue_ssl)
                st.success(i18n.t("zone_settings.generated_success").format(folder=data.get('folder_name', domain)))

                # Reactive stdout streaming sink for Terraform CLI logs
                log_window = st.empty()
                success, final_log = tf_runner.apply_site_with_healing(
                    domain=domain,
                    site_data=data,
                    site_dir=site_dir,
                    tf_env=tf_env,
                    cf_client=cf_client,
                    log_window=log_window,
                    generate_callback=template_engine.generate_tf_files,
                    global_defaults=global_defaults,
                    force_reissue_ssl=force_reissue_ssl,
                )

                if success:
                    st.balloons()
                    st.success(i18n.t("zone_settings.deploy_success").format(domain=domain))
                    cert_check_path = site_dir / "ssl" / "cert.pem"
                    if cert_check_path.exists():
                        st.info(i18n.t("zone_settings.deploy_cert_info").format(folder=data.get('folder_name', domain)))
                else:
                    st.error(i18n.t("zone_settings.deploy_error").format(domain=domain))

        st.info(i18n.t("zone_settings.process_completed"))


# ==============================================================================
# 3. TAB: ADD DOMAIN
# ==============================================================================
elif active_tab == "add_domain":
    st.title(i18n.t("add_domain.title"))

    with st.container(border=True):
        domain_input = st.text_input(i18n.t("add_domain.domain_label")).strip().lower()

        col1, col2 = st.columns(2)
        with col1:
            type_options = ["main", "redirect"]
            chosen_type = st.radio(
                i18n.t("add_domain.site_type_label"),
                options=type_options,
                format_func=lambda x: i18n.t(f"add_domain.type_{x}")
            )
            is_main = chosen_type == "main"

        with col2:
            if is_main:
                cms_options = {
                    "joomla": "Joomla",
                    "wordpress": "WordPress",
                    "prestashop_1_7": "PrestaShop 1.7 / 8.0+",
                    "prestashop_1_6": "PrestaShop 1.6",
                    "html/other": "HTML / Other",
                }
                cms_type = st.selectbox(i18n.t("add_domain.cms_label"), options=list(cms_options.keys()), format_func=lambda x: cms_options[x])
                target_site = None
                target_path = ""
            else:
                cms_type = None
                main_sites_list = [k for k, v in sites.items() if v.get("type") == "main"]
                if not main_sites_list:
                    st.error(i18n.t("add_domain.no_main_sites_err"))
                    target_site = None
                    target_path = ""
                else:
                    target_site = st.selectbox(i18n.t("add_domain.target_site_label"), main_sites_list)
                    target_path = st.text_input(i18n.t("add_domain.target_path_label")).strip()

        server_ip = st.text_input(i18n.t("add_domain.server_ip_label"))

        has_custom_ssl_choice = False
        if is_main:
            ssl_strategies = ["origin", "custom"]
            chosen_ssl = st.radio(
                i18n.t("add_domain.ssl_choice_label"),
                options=ssl_strategies,
                format_func=lambda x: i18n.t(f"add_domain.ssl_choice_{x}")
            )
            has_custom_ssl_choice = chosen_ssl == "custom"

        st.markdown("---")
        st.subheader(i18n.t("add_domain.cf_integration_title"))

        methods = ["auto", "manual"]
        creation_method = st.radio(
            i18n.t("add_domain.creation_method_label"),
            options=methods,
            format_func=lambda x: i18n.t(f"add_domain.method_{x}")
        )

        manual_zone_id = ""
        if creation_method == "manual":
            manual_zone_id = st.text_input(i18n.t("add_domain.manual_zone_input"))

        submit_domain = st.button(i18n.t("add_domain.submit_btn"), type="primary", use_container_width=True)
# Встроенная памятка для пользователя по дожиму безопасности
    with st.expander(i18n.t("add_domain.manual_hardening_expander")):
        st.markdown(i18n.t("add_domain.manual_hardening_content"))
    if submit_domain:
        if not domain_input or not server_ip:
            st.error(i18n.t("add_domain.err_empty_fields"))
        elif domain_input in sites:
            st.error(i18n.t("add_domain.err_domain_exists").format(domain=domain_input))
        else:
            # ARCHITECTURE: Standardize on ASCII Punycode for Cloudflare DNS zone registration
            try:
                domain_puny = domain_input.encode("idna").decode("utf-8")
            except Exception:
                domain_puny = domain_input

            zone_id = ""
            success_creation = True
            ns_list_for_ui = []

            if creation_method == "manual":
                if not manual_zone_id:
                    st.error(i18n.t("add_domain.err_manual_zone_empty"))
                    success_creation = False
                else:
                    zone_id = manual_zone_id
            else:
                with st.spinner("Provisioning zone via Cloudflare API..."):
                    if not cf_client:
                        st.error(i18n.t("add_domain.err_no_token"))
                        success_creation = False
                    else:
                        acc_id = cf_client.get_account_id()
                        if not acc_id:
                            st.error(i18n.t("add_domain.err_no_acc_id"))
                            success_creation = False
                        else:
                            is_ok, z_id, ns_list, msg = cf_client.create_zone(domain_puny, acc_id)
                            if is_ok:
                                zone_id = z_id
                                ns_list_for_ui = ns_list
                            else:
                                st.error(i18n.t("add_domain.err_cf_create").format(msg=msg))
                                success_creation = False

            if success_creation and zone_id:
                # Transliterate Cyrillic domain to Latin slug for OS filesystem paths
                folder_name = slugify_domain(domain_input)
                new_site_data = {
                    "type": "main" if is_main else "redirect",
                    "active": True,
                    "ip": server_ip,
                    "zone_id": zone_id,
                    "domain_puny": domain_puny,
                }

                if is_main:
                    new_site_data["cms"] = cms_type
                    new_site_data["folder_name"] = folder_name
                    new_site_data["has_custom_ssl"] = has_custom_ssl_choice
                    new_site_data["origin_ca"] = not has_custom_ssl_choice
                    new_site_data["custom_settings"] = {}
                else:
                    new_site_data["target_site"] = target_site
                    if target_path and not target_path.startswith("/"):
                        target_path = "/" + target_path
                    new_site_data["target_path"] = target_path
                    new_site_data["folder_name"] = f"redirect-{folder_name}"
                    new_site_data["custom_settings"] = {}

                # Atomic persistence to single source of truth (SSOT)
                st.session_state.config["sites"][domain_input] = new_site_data
                config_mgr.save_config(st.session_state.config)

                st.balloons()
                st.success(i18n.t("add_domain.success_domain_added").format(domain=domain_input))

                if ns_list_for_ui:
                    st.info(i18n.t("add_domain.ns_notice"))
                    st.code(ns_list_for_ui[0], language="text")
                    st.code(ns_list_for_ui[1], language="text")

                st.warning(i18n.t("add_domain.next_step_notice"))


# ==============================================================================
# 4. TAB: WAF & FIREWALL
# ==============================================================================
elif active_tab == "waf":
    st.title(i18n.t("waf.title"))

    main_sites_list = [k for k, v in sites.items() if v.get("type") == "main"]
    if not main_sites_list:
        st.warning(i18n.t("waf.no_main_sites"))
        st.stop()

    selected_site = st.selectbox(i18n.t("waf.select_site"), main_sites_list)
    site_data = sites[selected_site]
    cms = site_data.get("cms", "joomla").lower()
    custom_waf = site_data.get("custom_waf", {})

    st.markdown("---")

    # IP WHITELIST SECTION
    with st.container(border=True):
        st.subheader(i18n.t("waf.whitelist_title"))
        st.caption(i18n.t("waf.whitelist_caption"))

        current_ips = site_data.get("whitelist_ips", [])
        ips_str = ", ".join(current_ips) if current_ips else ""

        col_ip, col_btn = st.columns([3, 1])
        with col_ip:
            new_ips_input = st.text_input(i18n.t("waf.whitelist_input"), value=ips_str, placeholder="e.g. 198.51.100.10, 203.0.113.50")
        with col_btn:
            st.write("")
            st.write("")
            # Automated public IP discovery to streamline engineer onboarding
            if st.button(i18n.t("waf.my_ip_btn"), use_container_width=True):
                try:
                    my_ip = requests.get("https://api.ipify.org", timeout=2).text.strip()
                    if my_ip:
                        existing = [x.strip() for x in new_ips_input.split(",") if x.strip()]
                        if my_ip not in existing:
                            existing.append(my_ip)
                        new_ips_input = ", ".join(existing)
                        st.info(i18n.t("waf.my_ip_info").format(ip=my_ip))
                except Exception:
                    st.error(i18n.t("waf.my_ip_err"))

        if st.button(i18n.t("waf.whitelist_save_btn")):
            parsed_ips = [x.strip() for x in new_ips_input.split(",") if x.strip()]
            st.session_state.config["sites"][selected_site]["whitelist_ips"] = parsed_ips
            config_mgr.save_config(st.session_state.config)
            st.success(i18n.t("waf.whitelist_saved_success").format(domain=selected_site))
            st.rerun()

    # 5-RULE COMPREHENSIVE WAF SUITE
    st.markdown("---")
    st.subheader(i18n.t("waf.waf_rules_title").format(cms=cms.upper()))
    st.caption(i18n.t("waf.waf_rules_caption"))

    # ARCHITECTURE (Cloudflare Free Tier Expression Engine Constraints):
    # The Cloudflare Free plan strictly disallows regex evaluation operators (e.g. `matches`).
    # Passing regex operators returns HTTP 400 'cannot use matches operator with free plan'.
    # To maintain enterprise-grade security on Free tier zones, all signatures are decomposed
    # into chains of `contains`, `eq`, `lower()`, and grouped boolean logic (`and`, `or`, `not`).
    default_r2 = '(http.user_agent eq "") or (http.user_agent contains "python") or (http.user_agent contains "wget/") or (lower(http.request.uri.path) contains "/etc/passwd") or (lower(http.request.uri.query) contains "union select") or (lower(http.request.uri.query) contains "<script>") or (http.request.uri.path contains "../") or (http.request.uri.path contains "/.env") or (http.request.uri.path contains "/.git") or ( (lower(http.request.uri.path) contains ".sql" or lower(http.request.uri.path) contains ".bak" or lower(http.request.uri.path) contains ".log" or lower(http.request.uri.path) contains ".tar" or lower(http.request.uri.path) contains ".gz" or lower(http.request.uri.path) contains ".zip") and not (http.request.uri.path contains "/administrator/") and not (http.request.uri.path contains "/wp-admin/") and not (http.request.uri.query contains "controller=Admin") )'
    default_r3 = "(cf.threat_score gt 14)"

    # CMS-specific vulnerability signatures and admin panel protection
    if cms == "joomla":
        default_r4 = '(http.request.uri.path contains "/configuration.php") or (http.request.uri.path contains "htaccess.txt") or ( (lower(http.request.uri.path) contains "/images/") and (lower(http.request.uri.path) contains ".php") )'
        default_r5 = '(http.request.uri.path contains "/administrator/") and (http.request.method eq "GET")'
        default_rl = '(http.request.uri.path contains "/administrator/") and (http.request.method eq "POST")'
    elif cms == "wordpress":
        default_r4 = '(http.request.uri.path contains "/wp-config.php") or (http.request.uri.path contains "xmlrpc.php") or ( (lower(http.request.uri.path) contains "/wp-content/uploads/") and (lower(http.request.uri.path) contains ".php") )'
        default_r5 = '((http.request.uri.path contains "/wp-login.php") or (http.request.uri.path contains "/wp-admin/")) and (http.request.method eq "GET")'
        default_rl = '(http.request.uri.path contains "/wp-login.php")'
    else:
        default_r4 = '(http.request.uri.path contains "/app/config/parameters.php") or (http.request.uri.path contains "/vendor/") or (http.request.uri.path contains "/var/logs/")'
        default_r5 = '(http.request.uri.query contains "controller=AdminLogin") and (http.request.method eq "GET")'
        default_rl = '(http.request.uri.path contains "/login")'

    expr_label = i18n.t("waf.expression_label")

    with st.expander(i18n.t("waf.rule_1_title"), expanded=False):
        st.markdown(i18n.t("waf.rule_1_action"))
        st.write(i18n.t("waf.rule_1_desc"))

    with st.expander(i18n.t("waf.rule_2_title"), expanded=True):
        st.markdown(i18n.t("waf.rule_2_action"))
        r2_val = st.text_area(expr_label, value=custom_waf.get("rule_2", default_r2), height=180, key=f"r2_{selected_site}")

    with st.expander(i18n.t("waf.rule_3_title"), expanded=False):
        st.markdown(i18n.t("waf.rule_3_action"))
        r3_val = st.text_area(expr_label, value=custom_waf.get("rule_3", default_r3), height=70, key=f"r3_{selected_site}")

    r4_title = i18n.t("waf.rule_4_title").format(cms=cms.upper())
    with st.expander(r4_title, expanded=True):
        st.markdown(i18n.t("waf.rule_4_action"))
        r4_val = st.text_area(expr_label, value=custom_waf.get("rule_4", default_r4), height=120, key=f"r4_{selected_site}")

    with st.expander(i18n.t("waf.rule_5_title"), expanded=True):
        st.markdown(i18n.t("waf.rule_5_action"))
        r5_val = st.text_area(expr_label, value=custom_waf.get("rule_5", default_r5), height=80, key=f"r5_{selected_site}")

    with st.expander(i18n.t("waf.rate_limit_title"), expanded=False):
        st.markdown(i18n.t("waf.rate_limit_action"))
        rl_val = st.text_area(expr_label, value=custom_waf.get("rule_rate_limit", default_rl), height=80, key=f"rl_{selected_site}")

    col_save, col_reset = st.columns([2, 1])
    with col_save:
        if st.button(i18n.t("waf.save_waf_btn"), use_container_width=True):
            st.session_state.config["sites"][selected_site]["custom_waf"] = {
                "rule_2": r2_val,
                "rule_3": r3_val,
                "rule_4": r4_val,
                "rule_5": r5_val,
                "rule_rate_limit": rl_val,
            }
            config_mgr.save_config(st.session_state.config)
            st.success(i18n.t("waf.waf_saved_success").format(domain=selected_site))
            st.rerun()

    with col_reset:
        if st.button(i18n.t("waf.reset_waf_btn"), use_container_width=True):
            if "custom_waf" in st.session_state.config["sites"][selected_site]:
                del st.session_state.config["sites"][selected_site]["custom_waf"]
                config_mgr.save_config(st.session_state.config)
                st.info(i18n.t("waf.waf_reset_info"))
                st.rerun()

    st.markdown("---")
    st.subheader(i18n.t("waf.apply_waf_title").format(domain=selected_site))
    st.caption(i18n.t("waf.apply_waf_caption"))

    if st.button(i18n.t("waf.apply_waf_btn"), type="primary", use_container_width=True):
        st.session_state.config["sites"][selected_site]["custom_waf"] = {
            "rule_2": r2_val,
            "rule_3": r3_val,
            "rule_4": r4_val,
            "rule_5": r5_val,
            "rule_rate_limit": rl_val,
        }
        config_mgr.save_config(st.session_state.config)

        current_site_data = st.session_state.config["sites"][selected_site]
        tf_env = tf_runner.build_env(api_token)

        with st.spinner("Checking Terraform plugins..."):
            template_engine.ensure_shared_terraform(tf_runner, tf_env)

        with st.spinner("Deploying WAF..."):
            site_dir = template_engine.generate_tf_files(selected_site, current_site_data, global_defaults)
            log_win = st.empty()
            # Provision WAF updates through self-healing reconciliation loop
            success, final_log = tf_runner.apply_site_with_healing(
                domain=selected_site,
                site_data=current_site_data,
                site_dir=site_dir,
                tf_env=tf_env,
                cf_client=cf_client,
                log_window=log_win,
                generate_callback=template_engine.generate_tf_files,
                global_defaults=global_defaults,
            )
            if success:
                st.balloons()
                st.success(i18n.t("waf.apply_waf_success").format(domain=selected_site))
            else:
                st.error(i18n.t("waf.apply_waf_error"))


# ==============================================================================
# 5. TAB: ADMIN TOOLS (CACHE PURGE & CASCADE DECOMMISSIONING)
# ==============================================================================
elif active_tab == "tools":
    import shutil
    import time

    st.title(i18n.t("tools.title"))

    # --- 1. СБРОС КЭША (PURGE CACHE) ---
    with st.container(border=True):
        st.subheader(i18n.t("tools.purge_title"))
        st.caption(i18n.t("tools.purge_caption"))

        main_sites_list = [k for k, v in sites.items() if v.get("type") == "main"]
        PURGE_ALL_KEY = "__ALL_MAIN__"
        purge_targets = [PURGE_ALL_KEY] + main_sites_list

        def format_purge_target(x):
            return i18n.t("tools.all_main_sites") if x == PURGE_ALL_KEY else x

        selected_purge = st.selectbox(i18n.t("tools.select_purge_target"), purge_targets, format_func=format_purge_target)

        if st.button(i18n.t("tools.purge_btn"), type="primary"):
            if not cf_client:
                st.error(i18n.t("tools.no_token"))
            else:
                if selected_purge == PURGE_ALL_KEY:
                    zones_to_purge = [(k, v.get("zone_id")) for k, v in sites.items() if v.get("type") == "main"]
                else:
                    zones_to_purge = [(selected_purge, sites[selected_purge].get("zone_id"))]

                for domain_name, z_id in zones_to_purge:
                    if z_id:
                        ok, err = cf_client.purge_cache(z_id, purge_everything=True)
                        if ok:
                            st.success(i18n.t("tools.purge_success").format(domain=domain_name))
                        else:
                            st.error(i18n.t("tools.purge_err").format(domain=domain_name, err=err))
                st.balloons()

    # --- 2. ВЫВОД ИЗ ЭКСПЛУАТАЦИИ И КАСКАДНОЕ УДАЛЕНИЕ САЙТА ---
    st.markdown("---")
    with st.container(border=True):
        st.subheader(i18n.t("tools.decommission_title"))
        st.caption(i18n.t("tools.decommission_caption"))

        # Двухколоночный фильтр [3 : 1] точь-в-точь как в Настройках зоны
        col_del_sel, col_del_chk = st.columns([3, 1])
        with col_del_chk:
            st.write("")
            st.write("")
            del_show_redirects = st.checkbox(
                i18n.t("zone_settings.only_redirects"),
                key="del_redirect_filter_key",
            )

        with col_del_sel:
            if del_show_redirects:
                del_options = [k for k, v in sites.items() if v.get("type") == "redirect"]
            else:
                del_options = [k for k, v in sites.items() if v.get("type") == "main"]

            if not del_options:
                st.info(i18n.t("zone_settings.no_sites"))
                del_target = None
            else:
                del_target = st.selectbox(
                    i18n.t("tools.select_delete_target"),
                    del_options,
                    key="del_selector",
                )

        if del_target:
            del_data = sites.get(del_target, {})
            folder_name = del_data.get("folder_name", del_target)

            # Проверяем, привязаны ли к сайту зависимые зеркала (только для основных сайтов)
            bound_mirrors = [k for k, v in sites.items() if v.get("type") == "redirect" and v.get("target_site") == del_target]
            
            opt_cascade = False
            if bound_mirrors:
                st.warning(i18n.t("tools.mirrors_warning").format(mirrors=", ".join(bound_mirrors)))
                opt_cascade = st.checkbox(
                    i18n.t("tools.cascade_checkbox").format(mirrors=", ".join(bound_mirrors)),
                    value=True,
                )

            col_d1, col_d2 = st.columns(2)
            with col_d1:
                opt_tf = st.checkbox(i18n.t("tools.opt_destroy_tf"), value=True)
                opt_cf = st.checkbox(i18n.t("tools.opt_delete_zone_cf"), value=True)
            with col_d2:
                opt_files = st.checkbox(i18n.t("tools.opt_delete_files").format(folder=folder_name), value=True)

            confirm_input = st.text_input(
                i18n.t("tools.confirm_label").format(domain=del_target),
                placeholder=del_target,
            )

            # Запуск удаления с защитой от случайного клика
            if st.button(i18n.t("tools.delete_btn"), type="primary"):
                if confirm_input.strip() != del_target:
                    st.error(i18n.t("tools.err_confirm_mismatch"))
                else:
                    tf_env = tf_runner.build_env(api_token)
                    log_window = st.empty()

                    # Формируем очередь на удаление: либо только 1 сайт, либо сайт + все его зеркала
                    targets_to_purge = [del_target]
                    if opt_cascade and bound_mirrors:
                        targets_to_purge.extend(bound_mirrors)

                    for target_item in targets_to_purge:
                        t_data = st.session_state.config["sites"].get(target_item, {})
                        t_folder = t_data.get("folder_name", target_item)
                        t_site_dir = SITES_DIR / t_folder
                        t_zone_id = t_data.get("zone_id", "")

                        if target_item != del_target:
                            st.markdown(i18n.t("tools.cascade_destroying").format(domain=target_item))

                        # 1. Terraform destroy (снос DNS, WAF, SSL, Redirects)
                        if opt_tf and t_site_dir.exists():
                            with st.spinner(f"Terraform destroy: {target_item}..."):
                                tf_runner.destroy_site(t_site_dir, tf_env, log_window)

                        # 2. Удаление зоны из Cloudflare через REST API
                        if opt_cf and cf_client and t_zone_id:
                            with st.spinner(f"Deleting Cloudflare zone: {target_item}..."):
                                cf_ok, cf_err = cf_client.delete_zone(t_zone_id)
                                if not cf_ok:
                                    st.warning(f"Cloudflare zone error ({target_item}): {cf_err}")

                        # 3. Физическое удаление папки с диска
                        if opt_files and t_site_dir.exists():
                            try:
                                shutil.rmtree(t_site_dir)
                            except Exception as e:
                                st.warning(f"Could not remove folder ({target_item}): {e}")

                        # 4. Вычищение из базы данных config.yaml
                        if target_item in st.session_state.config["sites"]:
                            del st.session_state.config["sites"][target_item]

                    # Сохраняем обновленную базу данных
                    config_mgr.save_config(st.session_state.config)

                    st.balloons()
                    st.success(i18n.t("tools.delete_success").format(domain=del_target))
                    time.sleep(1.5)
                    st.rerun()