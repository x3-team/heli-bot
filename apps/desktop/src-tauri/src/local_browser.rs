//! Starts the user's Chrome or Edge with a DevTools port for the agent's browser tools.
//! A separate profile in the app data folder keeps the agent's logins between runs.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

/// Origins of the app webview: Windows, macOS/Linux, dev server.
const ALLOWED_ORIGINS: &str = "http://tauri.localhost,https://tauri.localhost,tauri://localhost,http://127.0.0.1:1420";

fn candidates() -> Vec<(&'static str, PathBuf)> {
    let mut out = Vec::new();
    #[cfg(target_os = "windows")]
    {
        let roots = [
            std::env::var("PROGRAMFILES").ok(),
            std::env::var("PROGRAMFILES(X86)").ok(),
            std::env::var("LOCALAPPDATA").ok(),
        ];
        for root in roots.iter().flatten() {
            out.push(("chrome", Path::new(root).join("Google\\Chrome\\Application\\chrome.exe")));
        }
        for root in roots.iter().flatten() {
            out.push(("edge", Path::new(root).join("Microsoft\\Edge\\Application\\msedge.exe")));
        }
    }
    #[cfg(target_os = "macos")]
    {
        out.push(("chrome", PathBuf::from("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")));
        out.push(("edge", PathBuf::from("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")));
    }
    #[cfg(target_os = "linux")]
    {
        for path in ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge"] {
            let kind = if path.contains("edge") { "edge" } else { "chrome" };
            out.push((kind, PathBuf::from(path)));
        }
    }
    out
}

fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|_| "no_free_port".to_string())?;
    let port = listener.local_addr().map_err(|_| "no_free_port".to_string())?.port();
    Ok(port)
}

pub fn launch(profile: &Path) -> Result<(String, u16), String> {
    let (kind, binary) = candidates()
        .into_iter()
        .find(|(_, path)| path.exists())
        .ok_or_else(|| "browser_binary_missing".to_string())?;
    std::fs::create_dir_all(profile).map_err(|_| "profile_dir_failed".to_string())?;
    let port = free_port()?;
    Command::new(&binary)
        .arg(format!("--remote-debugging-port={port}"))
        .arg(format!("--remote-allow-origins={ALLOWED_ORIGINS}"))
        .arg(format!("--user-data-dir={}", profile.display()))
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        // The user keeps working in other windows: the agent's window must not stall when it is covered or minimized.
        .arg("--disable-backgrounding-occluded-windows")
        .arg("--disable-renderer-backgrounding")
        .arg("--disable-background-timer-throttling")
        .arg("about:blank")
        .spawn()
        .map_err(|_| "browser_start_failed".to_string())?;
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if alive(port) {
            return Ok((kind.to_string(), port));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err("cdp_timeout".to_string())
}

fn http_get(port: u16, path: &str) -> Option<String> {
    let mut stream = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(800)).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok()?;
    let request = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;
    // DevTools may keep the socket open: stop once Content-Length bytes of body are in.
    let mut raw = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let n = match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => n,
            Err(_) => break,
        };
        raw.extend_from_slice(&chunk[..n]);
        let text = String::from_utf8_lossy(&raw);
        if let Some((head, body)) = text.split_once("\r\n\r\n") {
            let length = head
                .lines()
                .find_map(|l| l.to_ascii_lowercase().strip_prefix("content-length:").map(|v| v.trim().to_string()))
                .and_then(|v| v.parse::<usize>().ok());
            if length.is_some_and(|len| body.len() >= len) {
                break;
            }
        }
    }
    let text = String::from_utf8(raw).ok()?;
    Some(text.split_once("\r\n\r\n")?.1.to_string())
}

pub fn alive(port: u16) -> bool {
    http_get(port, "/json/version").is_some_and(|body| body.contains("webSocketDebuggerUrl"))
}

pub fn page_ws(port: u16) -> Result<String, String> {
    let body = http_get(port, "/json/list").ok_or_else(|| "browser_unreachable".to_string())?;
    pick_page(&body).ok_or_else(|| "no_page".to_string())
}

fn pick_page(body: &str) -> Option<String> {
    let targets: Vec<serde_json::Value> = serde_json::from_str(body).ok()?;
    targets
        .iter()
        .filter(|t| t["type"] == "page")
        .find(|t| {
            let url = t["url"].as_str().unwrap_or("");
            !url.starts_with("chrome://") && !url.starts_with("edge://") && !url.starts_with("devtools://")
        })
        .and_then(|t| t["webSocketDebuggerUrl"].as_str().map(str::to_string))
}

#[cfg(test)]
mod tests {
    use super::pick_page;

    #[test]
    fn picks_a_normal_tab() {
        let body = r#"[{"type":"page","url":"chrome://newtab-footer/","webSocketDebuggerUrl":"ws://x/1"},
            {"type":"browser_ui","url":"chrome://webui-toolbar","webSocketDebuggerUrl":"ws://x/2"},
            {"type":"page","url":"about:blank","webSocketDebuggerUrl":"ws://x/3"}]"#;
        assert_eq!(pick_page(body).as_deref(), Some("ws://x/3"));
    }
}
