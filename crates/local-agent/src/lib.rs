//! Local actions on the user's computer.
//!
//! Reads run immediately. Writes ask first unless the user turned that off.
//! Dangerous actions always ask. `run_command` stays off until the user enables it.
//! File and app names must sit on an allowlist. A stop flag refuses everything.

use std::path::{Component, Path};

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Risk {
    Read,
    Write,
    Danger,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Allow,
    Confirm,
    Deny,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Policy {
    pub run_command: bool,
    pub confirm_writes: bool,
    pub allowed_paths: Vec<String>,
    pub allowed_apps: Vec<String>,
    pub stopped: bool,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            run_command: false,
            confirm_writes: true,
            allowed_paths: Vec::new(),
            allowed_apps: Vec::new(),
            stopped: false,
        }
    }
}

impl Policy {
    pub fn stop(&mut self) {
        self.stopped = true;
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Action {
    pub tool: String,
    pub path: Option<String>,
    pub app: Option<String>,
}

impl Action {
    pub fn new(tool: impl Into<String>, path: Option<String>, app: Option<String>) -> Self {
        Self {
            tool: tool.into(),
            path,
            app,
        }
    }
}

pub fn risk_of(tool: &str) -> Risk {
    match tool {
        "screenshot" | "read_file" | "list_dir" => Risk::Read,
        "write_file" | "open_app" | "move_file" => Risk::Write,
        "delete_file" | "run_command" | "send_mail" | "pay" => Risk::Danger,
        _ => Risk::Danger,
    }
}

pub fn decide(policy: &Policy, action: &Action) -> Decision {
    if policy.stopped {
        return Decision::Deny;
    }
    if action.tool == "run_command" && !policy.run_command {
        return Decision::Deny;
    }
    if let Some(path) = &action.path {
        if !path_allowed(&policy.allowed_paths, path) {
            return Decision::Deny;
        }
    }
    if action.tool == "open_app" {
        let name = action.app.as_deref().unwrap_or("");
        if !policy.allowed_apps.iter().any(|app| app == name) {
            return Decision::Deny;
        }
    }
    match risk_of(&action.tool) {
        Risk::Read => Decision::Allow,
        Risk::Write if policy.confirm_writes => Decision::Confirm,
        Risk::Write => Decision::Allow,
        Risk::Danger => Decision::Confirm,
    }
}

fn path_allowed(allow: &[String], raw: &str) -> bool {
    let path = Path::new(raw);
    if path.components().any(|component| component == Component::ParentDir) {
        return false;
    }
    allow.iter().any(|root| path.starts_with(root))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> Policy {
        Policy {
            allowed_paths: vec!["C:/work".into(), "/Users/me/docs".into()],
            allowed_apps: vec!["notepad".into()],
            ..Policy::default()
        }
    }

    #[test]
    fn reads_run_and_writes_ask() {
        let rules = policy();
        assert_eq!(
            decide(&rules, &Action::new("screenshot", None, None)),
            Decision::Allow
        );
        assert_eq!(
            decide(&rules, &Action::new("read_file", Some("C:/work/a.txt".into()), None)),
            Decision::Allow
        );
        assert_eq!(
            decide(&rules, &Action::new("write_file", Some("C:/work/a.txt".into()), None)),
            Decision::Confirm
        );
    }

    #[test]
    fn writes_can_skip_confirmation_but_danger_cannot() {
        let mut rules = policy();
        rules.confirm_writes = false;
        assert_eq!(
            decide(&rules, &Action::new("write_file", Some("/Users/me/docs/a.txt".into()), None)),
            Decision::Allow
        );
        assert_eq!(
            decide(&rules, &Action::new("delete_file", Some("C:/work/a.txt".into()), None)),
            Decision::Confirm
        );
        assert_eq!(
            decide(&rules, &Action::new("pay", None, None)),
            Decision::Confirm
        );
    }

    #[test]
    fn run_command_is_off_until_enabled() {
        let mut rules = policy();
        assert_eq!(
            decide(&rules, &Action::new("run_command", None, None)),
            Decision::Deny
        );
        rules.run_command = true;
        assert_eq!(
            decide(&rules, &Action::new("run_command", None, None)),
            Decision::Confirm
        );
    }

    #[test]
    fn paths_and_apps_stay_on_the_allowlist() {
        let rules = policy();
        assert_eq!(
            decide(&rules, &Action::new("read_file", Some("C:/work/../secret".into()), None)),
            Decision::Deny
        );
        assert_eq!(
            decide(&rules, &Action::new("read_file", Some("D:/other/a.txt".into()), None)),
            Decision::Deny
        );
        assert_eq!(
            decide(&rules, &Action::new("open_app", None, Some("powershell".into()))),
            Decision::Deny
        );
        assert_eq!(
            decide(&rules, &Action::new("open_app", None, Some("notepad".into()))),
            Decision::Confirm
        );
    }

    #[test]
    fn stop_refuses_every_action() {
        let mut rules = policy();
        rules.stop();
        assert_eq!(
            decide(&rules, &Action::new("screenshot", None, None)),
            Decision::Deny
        );
        assert_eq!(
            decide(&rules, &Action::new("read_file", Some("C:/work/a.txt".into()), None)),
            Decision::Deny
        );
    }
}
