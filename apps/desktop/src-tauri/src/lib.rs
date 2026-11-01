use std::sync::atomic::{AtomicBool, Ordering};

use serde::Serialize;

static AGENT_STOPPED: AtomicBool = AtomicBool::new(false);
static LAUNCHED: std::sync::Mutex<Option<(String, u16)>> = std::sync::Mutex::new(None);

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
            browser_launch,
            browser_page_ws,
            log_agent_step,
        ])
        .run(tauri::generate_context!())
        .expect("failed to start Heli bot");
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
