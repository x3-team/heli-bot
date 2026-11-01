//! Files the agent hands over from Heli's cloud computer.
//!
//! They land in one folder only (Загрузки/Heli). Names are cleaned so a file cannot climb out
//! of it or take a reserved Windows name, nothing is overwritten, and files that would run code
//! are never opened by the app: the user only gets them shown in the folder.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

pub const MAX_BYTES: usize = 25 * 1024 * 1024;

const RESERVED: &[&str] = &[
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9", "lpt1",
    "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
];

/// Same list as the server (app/files.py RISKY_EXTENSIONS).
const RISKY: &[&str] = &[
    "exe", "msi", "msix", "msp", "appx", "appxbundle", "bat", "cmd", "com", "scr", "pif", "cpl", "ps1", "psm1",
    "ps1xml", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "lnk", "url", "scf", "reg", "inf", "jar", "msc", "dll",
    "sys", "gadget", "application", "chm", "sh", "command", "app", "appimage", "deb", "rpm", "pkg", "dmg", "iso",
    "img", "vhd", "vhdx",
];

/// A name that is safe on Windows, macOS and Linux: no folders, no reserved names, no odd characters.
pub fn safe_name(raw: &str) -> String {
    let last = raw.replace('\\', "/");
    let last = last.rsplit('/').next().unwrap_or("");
    let cleaned: String = last
        .chars()
        .map(|c| if c.is_control() || "<>:\"/\\|?*".contains(c) { '_' } else { c })
        .collect();
    let mut name = cleaned.split_whitespace().collect::<Vec<_>>().join(" ");
    name = name.trim_matches(|c: char| c == '.' || c == ' ').to_string();
    if name.is_empty() {
        name = "файл".into();
    }
    let (stem, ext) = split_ext(&name);
    let mut stem = stem.to_string();
    let ext: String = ext.chars().take(16).collect();
    let base = stem.split('.').next().unwrap_or("").to_lowercase();
    if RESERVED.contains(&base.as_str()) {
        stem = format!("_{stem}");
    }
    let budget = if ext.is_empty() { 120 } else { 120usize.saturating_sub(ext.chars().count() + 1) };
    let stem: String = stem.chars().take(budget).collect();
    if ext.is_empty() {
        stem
    } else {
        format!("{stem}.{ext}")
    }
}

fn split_ext(name: &str) -> (&str, &str) {
    match name.rfind('.') {
        Some(i) if i > 0 => (&name[..i], &name[i + 1..]),
        _ => (name, ""),
    }
}

pub fn is_risky(name: &str) -> bool {
    let (_, ext) = split_ext(name);
    RISKY.contains(&ext.to_lowercase().as_str())
}

/// Writes `data` into `dir` under a cleaned, unused name («отчёт (1).pdf» if «отчёт.pdf» exists).
pub fn save_into(dir: &Path, raw_name: &str, data: &[u8]) -> io::Result<PathBuf> {
    if data.len() > MAX_BYTES {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "file_too_large"));
    }
    fs::create_dir_all(dir)?;
    let name = safe_name(raw_name);
    let (stem, ext) = split_ext(&name);
    for n in 0..1000 {
        let candidate = match (n, ext.is_empty()) {
            (0, _) => name.clone(),
            (_, true) => format!("{stem} ({n})"),
            (_, false) => format!("{stem} ({n}).{ext}"),
        };
        let path = dir.join(candidate);
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut file) => {
                file.write_all(data)?;
                mark_from_internet(&path);
                return Ok(path);
            }
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(err),
        }
    }
    Err(io::Error::new(io::ErrorKind::AlreadyExists, "too_many_copies"))
}

/// True when `path` is a file directly inside `dir` (after resolving links and `..`).
pub fn is_inside(dir: &Path, path: &Path) -> bool {
    match (fs::canonicalize(dir), fs::canonicalize(path)) {
        (Ok(dir), Ok(path)) => path.parent() == Some(dir.as_path()) && path.is_file(),
        _ => false,
    }
}

/// Windows: the «downloaded from the internet» mark, so Office opens the file in protected
/// view and SmartScreen warns before anything runs. Elsewhere there is nothing to do.
#[cfg(windows)]
fn mark_from_internet(path: &Path) {
    let mut stream = path.as_os_str().to_owned();
    stream.push(":Zone.Identifier");
    let _ = fs::write(PathBuf::from(stream), "[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://getheli.ru/\r\n");
}

#[cfg(not(windows))]
fn mark_from_internet(_path: &Path) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_stay_in_the_folder() {
        assert_eq!(safe_name("отчёт.pdf"), "отчёт.pdf");
        assert_eq!(safe_name("../../etc/passwd"), "passwd");
        assert_eq!(safe_name("C:\\Windows\\evil.dll"), "evil.dll");
        assert_eq!(safe_name("a<b>c:\"d|e?f*.txt"), "a_b_c__d_e_f_.txt");
        assert_eq!(safe_name("CON.txt"), "_CON.txt");
        assert_eq!(safe_name("  .hidden. "), "hidden");
        assert_eq!(safe_name(""), "файл");
        assert_eq!(safe_name(".."), "файл");
        let long = format!("{}.xlsx", "x".repeat(300));
        assert_eq!(safe_name(&long), format!("{}.xlsx", "x".repeat(115)));
    }

    #[test]
    fn risky_files_are_known() {
        assert!(is_risky("setup.EXE"));
        assert!(is_risky("run.ps1"));
        assert!(!is_risky("отчёт.pdf"));
        assert!(!is_risky("exe"));
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("heli-files-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn saves_without_overwriting() {
        let dir = temp_dir("save");
        let first = save_into(&dir, "счёт.pdf", b"one").unwrap();
        let second = save_into(&dir, "счёт.pdf", b"two").unwrap();
        let third = save_into(&dir, "../счёт.pdf", b"three").unwrap();
        assert_eq!(first.file_name().unwrap(), "счёт.pdf");
        assert_eq!(second.file_name().unwrap(), "счёт (1).pdf");
        assert_eq!(third.file_name().unwrap(), "счёт (2).pdf");
        assert_eq!(fs::read(&first).unwrap(), b"one");
        assert!(is_inside(&dir, &second));
        assert!(!is_inside(&dir, &dir.join("..").join("x")));
        assert!(!is_inside(&dir, Path::new("/etc/passwd")));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn refuses_huge_files() {
        let dir = temp_dir("huge");
        let data = vec![0u8; MAX_BYTES + 1];
        assert!(save_into(&dir, "big.bin", &data).is_err());
    }
}
