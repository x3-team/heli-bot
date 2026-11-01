//! Pointer and keyboard actions the vision model may ask for.
//!
//! Coordinates are on a 0..=1000 grid over the primary screen, so the model never
//! needs the real resolution and DPI scaling stays a desktop concern.

use serde::{Deserialize, Serialize};

use crate::{Decision, Policy, Risk};

pub const GRID: u32 = 1000;
pub const TEXT_LIMIT: usize = 2000;
pub const SCROLL_LIMIT: i32 = 15;
pub const WAIT_LIMIT: u32 = 10;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ComputerAction {
    Click { x: u32, y: u32 },
    DoubleClick { x: u32, y: u32 },
    RightClick { x: u32, y: u32 },
    Type {
        text: String,
        #[serde(default)]
        x: Option<u32>,
        #[serde(default)]
        y: Option<u32>,
    },
    Key { keys: String },
    Scroll { x: u32, y: u32, amount: i32 },
    Wait { seconds: u32 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Modifier {
    Ctrl,
    Alt,
    Shift,
    Win,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NamedKey {
    Enter,
    Tab,
    Escape,
    Backspace,
    Delete,
    Up,
    Down,
    Left,
    Right,
    Home,
    End,
    PageUp,
    PageDown,
    Space,
    F(u8),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyName {
    Named(NamedKey),
    Char(char),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyCombo {
    pub modifiers: Vec<Modifier>,
    pub key: KeyName,
}

impl ComputerAction {
    pub fn tool(&self) -> &'static str {
        match self {
            Self::Click { .. } => "click",
            Self::DoubleClick { .. } => "double_click",
            Self::RightClick { .. } => "right_click",
            Self::Type { .. } => "type",
            Self::Key { .. } => "key",
            Self::Scroll { .. } => "scroll",
            Self::Wait { .. } => "wait",
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        match self {
            Self::Click { x, y } | Self::DoubleClick { x, y } | Self::RightClick { x, y } => point(*x, *y),
            Self::Type { text, x, y } => {
                if text.is_empty() || text.chars().count() > TEXT_LIMIT {
                    return Err("bad_text".into());
                }
                match (x, y) {
                    (Some(x), Some(y)) => point(*x, *y),
                    (None, None) => Ok(()),
                    _ => Err("bad_point".into()),
                }
            }
            Self::Key { keys } => parse_keys(keys).map(|_| ()),
            Self::Scroll { x, y, amount } => {
                point(*x, *y)?;
                if *amount == 0 || amount.abs() > SCROLL_LIMIT {
                    return Err("bad_scroll".into());
                }
                Ok(())
            }
            Self::Wait { seconds } => {
                if *seconds == 0 || *seconds > WAIT_LIMIT {
                    return Err("bad_wait".into());
                }
                Ok(())
            }
        }
    }

    /// Scroll and wait change nothing. Combos that close windows or open system
    /// dialogs count as dangerous, as does anything the model itself flagged.
    pub fn risk(&self, flagged: bool) -> Risk {
        if flagged {
            return Risk::Danger;
        }
        match self {
            Self::Wait { .. } | Self::Scroll { .. } => Risk::Read,
            Self::Key { keys } => match parse_keys(keys) {
                Ok(combo) if combo.dangerous() => Risk::Danger,
                Ok(_) => Risk::Write,
                Err(_) => Risk::Danger,
            },
            _ => Risk::Write,
        }
    }
}

impl KeyCombo {
    pub fn dangerous(&self) -> bool {
        let has = |m: Modifier| self.modifiers.contains(&m);
        if has(Modifier::Win) {
            return true;
        }
        match self.key {
            KeyName::Named(NamedKey::F(4)) if has(Modifier::Alt) => true,
            KeyName::Named(NamedKey::Delete) => has(Modifier::Shift) || (has(Modifier::Ctrl) && has(Modifier::Alt)),
            KeyName::Named(NamedKey::Escape) => has(Modifier::Ctrl) && has(Modifier::Shift),
            KeyName::Char('w') | KeyName::Char('q') => has(Modifier::Ctrl),
            _ => false,
        }
    }
}

/// Decide whether an action runs now, needs the user's OK, or is refused.
pub fn decide_action(policy: &Policy, action: &ComputerAction, flagged: bool) -> Decision {
    if policy.stopped || action.validate().is_err() {
        return Decision::Deny;
    }
    match action.risk(flagged) {
        Risk::Read => Decision::Allow,
        Risk::Write if policy.confirm_writes => Decision::Confirm,
        Risk::Write => Decision::Allow,
        Risk::Danger => Decision::Confirm,
    }
}

/// Map a 0..=1000 grid value onto `size` pixels.
pub fn to_pixels(value: u32, size: i32) -> i32 {
    if size <= 1 {
        return 0;
    }
    let value = value.min(GRID) as i64;
    ((value * (size as i64 - 1) + (GRID as i64 / 2)) / GRID as i64) as i32
}

pub fn parse_keys(raw: &str) -> Result<KeyCombo, String> {
    let lower = raw.trim().to_lowercase();
    if lower.is_empty() || lower.len() > 40 {
        return Err("bad_keys".into());
    }
    let parts: Vec<&str> = lower.split('+').map(str::trim).collect();
    let (last, mods) = parts.split_last().ok_or("bad_keys")?;
    let mut modifiers = Vec::new();
    for part in mods {
        let modifier = match *part {
            "ctrl" | "control" => Modifier::Ctrl,
            "alt" => Modifier::Alt,
            "shift" => Modifier::Shift,
            "win" | "meta" | "cmd" | "super" => Modifier::Win,
            _ => return Err("bad_keys".into()),
        };
        if modifiers.contains(&modifier) {
            return Err("bad_keys".into());
        }
        modifiers.push(modifier);
    }
    let key = match *last {
        "enter" | "return" => KeyName::Named(NamedKey::Enter),
        "tab" => KeyName::Named(NamedKey::Tab),
        "esc" | "escape" => KeyName::Named(NamedKey::Escape),
        "backspace" => KeyName::Named(NamedKey::Backspace),
        "delete" | "del" => KeyName::Named(NamedKey::Delete),
        "up" => KeyName::Named(NamedKey::Up),
        "down" => KeyName::Named(NamedKey::Down),
        "left" => KeyName::Named(NamedKey::Left),
        "right" => KeyName::Named(NamedKey::Right),
        "home" => KeyName::Named(NamedKey::Home),
        "end" => KeyName::Named(NamedKey::End),
        "pageup" => KeyName::Named(NamedKey::PageUp),
        "pagedown" => KeyName::Named(NamedKey::PageDown),
        "space" => KeyName::Named(NamedKey::Space),
        other => {
            if let Some(n) = other.strip_prefix('f').and_then(|n| n.parse::<u8>().ok()) {
                if (1..=12).contains(&n) {
                    KeyName::Named(NamedKey::F(n))
                } else {
                    return Err("bad_keys".into());
                }
            } else {
                let mut chars = other.chars();
                match (chars.next(), chars.next()) {
                    (Some(c), None) if c.is_ascii_alphanumeric() => KeyName::Char(c),
                    _ => return Err("bad_keys".into()),
                }
            }
        }
    };
    Ok(KeyCombo { modifiers, key })
}

fn point(x: u32, y: u32) -> Result<(), String> {
    if x > GRID || y > GRID {
        return Err("bad_point".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grid_maps_onto_screen_pixels() {
        assert_eq!(to_pixels(0, 1920), 0);
        assert_eq!(to_pixels(1000, 1920), 1919);
        assert_eq!(to_pixels(500, 1920), 960);
        assert_eq!(to_pixels(2000, 1080), 1079);
        assert_eq!(to_pixels(500, 0), 0);
    }

    #[test]
    fn validation_rejects_out_of_grid_and_empty_input() {
        assert!(ComputerAction::Click { x: 1001, y: 5 }.validate().is_err());
        assert!(ComputerAction::Type { text: String::new(), x: None, y: None }.validate().is_err());
        assert!(ComputerAction::Type { text: "Привет".into(), x: Some(3), y: None }.validate().is_err());
        assert!(ComputerAction::Scroll { x: 1, y: 1, amount: 0 }.validate().is_err());
        assert!(ComputerAction::Wait { seconds: 60 }.validate().is_err());
        assert!(ComputerAction::Key { keys: "ctrl+ctrl+s".into() }.validate().is_err());
        assert!(ComputerAction::Key { keys: "hyper+s".into() }.validate().is_err());
        assert!(ComputerAction::Type { text: "Привет".into(), x: Some(3), y: Some(4) }.validate().is_ok());
    }

    #[test]
    fn keys_parse_into_combos() {
        let combo = parse_keys("Ctrl+Shift+S").unwrap();
        assert_eq!(combo.modifiers, vec![Modifier::Ctrl, Modifier::Shift]);
        assert_eq!(combo.key, KeyName::Char('s'));
        assert_eq!(parse_keys("enter").unwrap().key, KeyName::Named(NamedKey::Enter));
        assert_eq!(parse_keys("f5").unwrap().key, KeyName::Named(NamedKey::F(5)));
        assert!(parse_keys("f13").is_err());
        assert!(parse_keys("ж").is_err());
    }

    #[test]
    fn closing_and_system_combos_are_dangerous() {
        for keys in ["alt+f4", "win+r", "shift+delete", "ctrl+alt+delete", "ctrl+w", "ctrl+shift+esc"] {
            let action = ComputerAction::Key { keys: keys.into() };
            assert_eq!(action.risk(false), Risk::Danger, "{keys}");
        }
        assert_eq!(ComputerAction::Key { keys: "ctrl+s".into() }.risk(false), Risk::Write);
        assert_eq!(ComputerAction::Scroll { x: 1, y: 1, amount: -3 }.risk(false), Risk::Read);
        assert_eq!(ComputerAction::Click { x: 1, y: 1 }.risk(true), Risk::Danger);
    }

    #[test]
    fn writes_ask_and_danger_always_asks() {
        let mut policy = Policy::default();
        let click = ComputerAction::Click { x: 10, y: 10 };
        assert_eq!(decide_action(&policy, &click, false), Decision::Confirm);
        policy.confirm_writes = false;
        assert_eq!(decide_action(&policy, &click, false), Decision::Allow);
        assert_eq!(decide_action(&policy, &click, true), Decision::Confirm);
        assert_eq!(
            decide_action(&policy, &ComputerAction::Wait { seconds: 2 }, false),
            Decision::Allow
        );
        assert_eq!(
            decide_action(&policy, &ComputerAction::Click { x: 5000, y: 1 }, false),
            Decision::Deny
        );
        policy.stop();
        assert_eq!(
            decide_action(&policy, &ComputerAction::Wait { seconds: 2 }, false),
            Decision::Deny
        );
    }
}
