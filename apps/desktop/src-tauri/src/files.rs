//! Files from Heli's cloud computer: saved into Загрузки/Heli, opened or shown on the user's request.
//!
//! The app writes only into that folder, never overwrites, and never opens a file that would run
//! code (.exe, .bat, .ps1, …): those are only shown in the folder.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Serialize;
use tauri::Manager;

use local_agent::files;

#[derive(Serialize)]
pub struct SavedFile {
    path: String,
    name: String,
    folder: String,
    risky: bool,
}

fn heli_folder(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let downloads = app.path().download_dir().map_err(|_| "no_downloads_folder".to_string())?;
    Ok(downloads.join("Heli"))
}

/// `%D0%BE.pdf` → `о.pdf` (the webview sends the name URL-encoded in a header).
fn percent_decode(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
            if let Ok(byte) = u8::from_str_radix(hex, 16) {
                out.push(byte);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Body: the file's bytes. Header x-file-name: its URL-encoded name.
#[tauri::command]
pub fn file_save(app: tauri::AppHandle, request: tauri::ipc::Request<'_>) -> Result<SavedFile, String> {
    let tauri::ipc::InvokeBody::Raw(data) = request.body() else {
        return Err("bad_body".into());
    };
    let name = request
        .headers()
        .get("x-file-name")
        .and_then(|value| value.to_str().ok())
        .map(percent_decode)
        .unwrap_or_default();
    let folder = heli_folder(&app)?;
    let path = files::save_into(&folder, &name, data).map_err(|err| err.to_string())?;
    let saved = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    Ok(SavedFile {
        risky: files::is_risky(&saved),
        name: saved,
        path: path.to_string_lossy().into_owned(),
        folder: folder.to_string_lossy().into_owned(),
    })
}

fn checked(app: &tauri::AppHandle, path: &str) -> Result<PathBuf, String> {
    let folder = heli_folder(app)?;
    let path = PathBuf::from(path);
    if !files::is_inside(&folder, &path) {
        return Err("outside_heli_folder".into());
    }
    Ok(path)
}

/// Opens a saved file in its usual program (PDF viewer, Excel, …). Never one that runs code.
#[tauri::command]
pub fn file_open(app: tauri::AppHandle, path: String) -> Result<(), String> {
    let path = checked(&app, &path)?;
    if files::is_risky(&path.to_string_lossy()) {
        return Err("risky_file".into());
    }
    open_default(&path)
}

/// Shows the file selected in Explorer / Finder (or just the Heli folder).
#[tauri::command]
pub fn file_reveal(app: tauri::AppHandle, path: Option<String>) -> Result<(), String> {
    let folder = heli_folder(&app)?;
    std::fs::create_dir_all(&folder).map_err(|err| err.to_string())?;
    match path {
        Some(path) => reveal(&checked(&app, &path)?),
        None => open_default(&folder),
    }
}

#[cfg(windows)]
fn open_default(path: &Path) -> Result<(), String> {
    Command::new("explorer.exe").arg(path).spawn().map(|_| ()).map_err(|err| err.to_string())
}

#[cfg(windows)]
fn reveal(path: &Path) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    // Windows paths cannot contain quotes, so this cannot be broken out of.
    Command::new("explorer.exe")
        .raw_arg(format!("/select,\"{}\"", path.display()))
        .spawn()
        .map(|_| ())
        .map_err(|err| err.to_string())
}

#[cfg(target_os = "macos")]
fn open_default(path: &Path) -> Result<(), String> {
    Command::new("open").arg(path).spawn().map(|_| ()).map_err(|err| err.to_string())
}

#[cfg(target_os = "macos")]
fn reveal(path: &Path) -> Result<(), String> {
    Command::new("open").arg("-R").arg(path).spawn().map(|_| ()).map_err(|err| err.to_string())
}

#[cfg(all(not(windows), not(target_os = "macos")))]
fn open_default(path: &Path) -> Result<(), String> {
    Command::new("xdg-open").arg(path).spawn().map(|_| ()).map_err(|err| err.to_string())
}

#[cfg(all(not(windows), not(target_os = "macos")))]
fn reveal(path: &Path) -> Result<(), String> {
    open_default(path.parent().unwrap_or(path))
}

#[cfg(test)]
mod tests {
    use super::percent_decode;

    #[test]
    fn decodes_names() {
        assert_eq!(percent_decode("%D0%BE%D1%82%D1%87%D1%91%D1%82.pdf"), "отчёт.pdf");
        assert_eq!(percent_decode("a%20b%"), "a b%");
        assert_eq!(percent_decode("%zz"), "%zz");
    }
}
