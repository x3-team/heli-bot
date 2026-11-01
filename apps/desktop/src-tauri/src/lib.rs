use std::sync::atomic::{AtomicBool, Ordering};

use serde::Serialize;

static AGENT_STOPPED: AtomicBool = AtomicBool::new(false);
static LAUNCHED: std::sync::Mutex<Option<(String, u16)>> = std::sync::Mutex::new(None);

mod files;
mod local_browser;

#[derive(Serialize)]
struct BrowserLaunch {
    browser: String,
    port: u16,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            agent_decide,
            agent_set_stopped,
            computer_screenshot,
            computer_click,
            computer_type,
            computer_shot,
            computer_decide,
            computer_act,
            browser_launch,
            browser_page_ws,
            log_agent_step,
            files::file_save,
            files::file_open,
            files::file_reveal,
            link_open,
        ])
        .run(tauri::generate_context!())
        .expect("failed to start Heli bot");
}

/// Only plain web and mail links from chat replies; nothing that could start a program.
fn safe_link(url: &str) -> bool {
    let lower = url.to_ascii_lowercase();
    url.len() <= 4096
        && (lower.starts_with("https://") || lower.starts_with("http://") || lower.starts_with("mailto:"))
        && !url.chars().any(|c| c.is_whitespace() || c.is_control() || c == '"')
}

/// Opens a link from a chat reply in the system browser.
#[tauri::command]
fn link_open(url: String) -> Result<(), String> {
    if !safe_link(&url) {
        return Err("bad_link".into());
    }
    #[cfg(windows)]
    let mut command = {
        let mut c = std::process::Command::new("rundll32.exe");
        c.args(["url.dll,FileProtocolHandler", &url]);
        c
    };
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut c = std::process::Command::new("open");
        c.arg(&url);
        c
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = {
        let mut c = std::process::Command::new("xdg-open");
        c.arg(&url);
        c
    };
    command.spawn().map(|_| ()).map_err(|err| err.to_string())
}

#[tauri::command]
fn agent_set_stopped(stopped: bool) {
    AGENT_STOPPED.store(stopped, Ordering::SeqCst);
}

#[tauri::command]
fn agent_decide(
    tool: String,
    path: Option<String>,
    app: Option<String>,
    run_command: bool,
    confirm_writes: bool,
    allowed_paths: Vec<String>,
    allowed_apps: Vec<String>,
    stopped: bool,
) -> String {
    let halted = stopped || AGENT_STOPPED.load(Ordering::SeqCst);
    let policy = local_agent::Policy {
        run_command,
        confirm_writes,
        allowed_paths,
        allowed_apps,
        stopped: halted,
    };
    let action = local_agent::Action::new(tool, path, app);
    match local_agent::decide(&policy, &action) {
        local_agent::Decision::Allow => "allow",
        local_agent::Decision::Confirm => "confirm",
        local_agent::Decision::Deny => "deny",
    }
    .to_string()
}

#[tauri::command]
fn computer_screenshot() -> Result<String, String> {
    local_agent::computer::screenshot_png_base64()
}

#[tauri::command]
fn computer_click(x: i32, y: i32) -> Result<(), String> {
    local_agent::computer::pointer_click(x, y)
}

#[tauri::command]
fn computer_type(text: String) -> Result<(), String> {
    local_agent::computer::type_text(&text)
}

#[tauri::command]
fn computer_shot() -> Result<local_agent::computer::Shot, String> {
    if AGENT_STOPPED.load(Ordering::SeqCst) {
        return Err("stopped".into());
    }
    local_agent::computer::screenshot_for_model()
}

fn computer_policy(confirm_writes: bool) -> local_agent::Policy {
    local_agent::Policy {
        confirm_writes,
        stopped: AGENT_STOPPED.load(Ordering::SeqCst),
        ..local_agent::Policy::default()
    }
}

fn decision_name(decision: local_agent::Decision) -> &'static str {
    match decision {
        local_agent::Decision::Allow => "allow",
        local_agent::Decision::Confirm => "confirm",
        local_agent::Decision::Deny => "deny",
    }
}

#[tauri::command]
fn computer_decide(action: local_agent::actions::ComputerAction, flagged: bool, confirm_writes: bool) -> String {
    let policy = computer_policy(confirm_writes);
    decision_name(local_agent::actions::decide_action(&policy, &action, flagged)).to_string()
}

/// Runs one pointer/keyboard action. An action that needs the user's OK runs only
/// with `approved`, which the UI sets after the confirmation card.
#[tauri::command]
async fn computer_act(
    action: local_agent::actions::ComputerAction,
    flagged: bool,
    confirm_writes: bool,
    approved: bool,
) -> Result<(), String> {
    let policy = computer_policy(confirm_writes);
    match local_agent::actions::decide_action(&policy, &action, flagged) {
        local_agent::Decision::Deny if policy.stopped => return Err("stopped".into()),
        local_agent::Decision::Deny => return Err("denied".into()),
        local_agent::Decision::Confirm if !approved => return Err("needs_confirmation".into()),
        _ => {}
    }
    tauri::async_runtime::spawn_blocking(move || {
        if AGENT_STOPPED.load(Ordering::SeqCst) {
            return Err("stopped".to_string());
        }
        local_agent::computer::perform(&action)
    })
    .await
    .map_err(|err| err.to_string())?
}

#[tauri::command]
fn browser_launch(app: tauri::AppHandle) -> Result<BrowserLaunch, String> {
    if let Ok(port) = std::env::var("HELI_LOCAL_BROWSER_PORT") {
        let port: u16 = port.parse().map_err(|_| "bad_port".to_string())?;
        let browser = std::env::var("HELI_LOCAL_BROWSER").unwrap_or_else(|_| "chrome".into());
        return Ok(BrowserLaunch { browser, port });
    }
    use tauri::Manager;
    let profile = app
        .path()
        .app_local_data_dir()
        .map_err(|_| "no_data_dir".to_string())?
        .join("browser-profile");
    let mut guard = LAUNCHED.lock().map_err(|_| "lock".to_string())?;
    if let Some((browser, port)) = guard.clone() {
        if local_browser::alive(port) {
            return Ok(BrowserLaunch { browser, port });
        }
    }
    let (browser, port) = local_browser::launch(&profile)?;
    *guard = Some((browser.clone(), port));
    Ok(BrowserLaunch { browser, port })
}

/// DevTools websocket of the first normal tab (the webview cannot read /json itself: no CORS there).
#[tauri::command]
fn browser_page_ws(port: u16) -> Result<String, String> {
    local_browser::page_ws(port)
}

#[tauri::command]
fn log_agent_step(tool: String, screenshot_base64: Option<String>) -> Result<(), String> {
    log::info!(
        "agent step tool={} screenshot={}",
        tool,
        screenshot_base64.as_ref().map(|s| s.len()).unwrap_or(0)
    );
    Ok(())
}

#[cfg(test)]
mod link_tests {
    use super::safe_link;

    #[test]
    fn only_web_and_mail_links() {
        assert!(safe_link("https://vychisli.example/gpu?a=1&b=2"));
        assert!(safe_link("http://example.com"));
        assert!(safe_link("mailto:hi@example.com"));
        assert!(!safe_link("file:///C:/Windows/System32/calc.exe"));
        assert!(!safe_link("javascript:alert(1)"));
        assert!(!safe_link("https://a.example/ x"));
        assert!(!safe_link("https://a.example/\"x"));
        assert!(!safe_link("C:\\Windows\\calc.exe"));
    }
}
