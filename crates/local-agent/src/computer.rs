//! Screen capture and pointer/keyboard input on the user's machine.

use std::{thread, time::Duration};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use enigo::{Axis, Button, Coordinate, Direction, Enigo, Key, Keyboard, Mouse, Settings};
use png::{BitDepth, ColorType, Encoder};
use screenshots::Screen;
use serde::Serialize;

use crate::actions::{parse_keys, to_pixels, ComputerAction, KeyName, Modifier, NamedKey};
use crate::scale::downscale;

/// Longest side of the image sent to the vision model. Smaller is cheaper and
/// the 0..=1000 grid keeps clicks precise enough for buttons and fields.
pub const SHOT_MAX_SIDE: u32 = 1280;

#[derive(Debug, Serialize)]
pub struct Shot {
    pub png_base64: String,
    pub width: u32,
    pub height: u32,
}

fn primary_screen() -> Result<Screen, String> {
    let screens = Screen::all().map_err(|err| err.to_string())?;
    let primary = screens.iter().position(|s| s.display_info.is_primary).unwrap_or(0);
    screens.into_iter().nth(primary).ok_or_else(|| "no_monitor".to_string())
}

/// Full-resolution PNG of the primary screen, kept for the step log.
pub fn screenshot_png_base64() -> Result<String, String> {
    let shot = primary_screen()?.capture().map_err(|err| err.to_string())?;
    let (width, height) = (shot.width(), shot.height());
    encode_png(width, height, shot.as_raw()).map(|buf| STANDARD.encode(buf))
}

/// Primary screen scaled down for the vision model.
pub fn screenshot_for_model() -> Result<Shot, String> {
    let shot = primary_screen()?.capture().map_err(|err| err.to_string())?;
    let (width, height) = (shot.width(), shot.height());
    let (w, h, pixels) = downscale(width, height, shot.as_raw(), SHOT_MAX_SIDE);
    let buf = encode_png(w, h, &pixels)?;
    Ok(Shot {
        png_base64: STANDARD.encode(buf),
        width: w,
        height: h,
    })
}

fn encode_png(width: u32, height: u32, rgba: &[u8]) -> Result<Vec<u8>, String> {
    let mut buf = Vec::new();
    {
        let mut encoder = Encoder::new(&mut buf, width, height);
        encoder.set_color(ColorType::Rgba);
        encoder.set_depth(BitDepth::Eight);
        let mut writer = encoder.write_header().map_err(|err| err.to_string())?;
        writer.write_image_data(rgba).map_err(|err| err.to_string())?;
    }
    Ok(buf)
}

pub fn pointer_click(x: i32, y: i32) -> Result<(), String> {
    let mut enigo = Enigo::new(&Settings::default()).map_err(|err| err.to_string())?;
    enigo
        .move_mouse(x, y, Coordinate::Abs)
        .map_err(|err| err.to_string())?;
    enigo
        .button(Button::Left, Direction::Click)
        .map_err(|err| err.to_string())
}

pub fn type_text(text: &str) -> Result<(), String> {
    let mut enigo = Enigo::new(&Settings::default()).map_err(|err| err.to_string())?;
    enigo.text(text).map_err(|err| err.to_string())
}

/// Run one validated action. Grid points map onto the main display in the
/// coordinate space enigo uses, so DPI scaling is handled by the OS layer.
pub fn perform(action: &ComputerAction) -> Result<(), String> {
    action.validate()?;
    let mut enigo = Enigo::new(&Settings::default()).map_err(|err| err.to_string())?;
    let (sw, sh) = enigo.main_display().map_err(|err| err.to_string())?;
    let go = |enigo: &mut Enigo, x: u32, y: u32| -> Result<(), String> {
        enigo
            .move_mouse(to_pixels(x, sw), to_pixels(y, sh), Coordinate::Abs)
            .map_err(|err| err.to_string())
    };
    let err = |e: enigo::InputError| e.to_string();
    match action {
        ComputerAction::Click { x, y } => {
            go(&mut enigo, *x, *y)?;
            enigo.button(Button::Left, Direction::Click).map_err(err)
        }
        ComputerAction::DoubleClick { x, y } => {
            go(&mut enigo, *x, *y)?;
            enigo.button(Button::Left, Direction::Click).map_err(err)?;
            thread::sleep(Duration::from_millis(60));
            enigo.button(Button::Left, Direction::Click).map_err(err)
        }
        ComputerAction::RightClick { x, y } => {
            go(&mut enigo, *x, *y)?;
            enigo.button(Button::Right, Direction::Click).map_err(err)
        }
        ComputerAction::Type { text, x, y } => {
            if let (Some(x), Some(y)) = (x, y) {
                go(&mut enigo, *x, *y)?;
                enigo.button(Button::Left, Direction::Click).map_err(err)?;
                thread::sleep(Duration::from_millis(120));
            }
            enigo.text(text).map_err(err)
        }
        ComputerAction::Key { keys } => {
            let combo = parse_keys(keys)?;
            let mods: Vec<Key> = combo.modifiers.iter().map(|m| modifier_key(*m)).collect();
            for key in &mods {
                enigo.key(*key, Direction::Press).map_err(err)?;
            }
            let result = enigo.key(main_key(combo.key), Direction::Click).map_err(err);
            for key in mods.iter().rev() {
                let _ = enigo.key(*key, Direction::Release);
            }
            result
        }
        ComputerAction::Scroll { x, y, amount } => {
            go(&mut enigo, *x, *y)?;
            enigo.scroll(*amount, Axis::Vertical).map_err(err)
        }
        ComputerAction::Wait { seconds } => {
            thread::sleep(Duration::from_secs(*seconds as u64));
            Ok(())
        }
    }
}

fn modifier_key(modifier: Modifier) -> Key {
    match modifier {
        Modifier::Ctrl => Key::Control,
        Modifier::Alt => Key::Alt,
        Modifier::Shift => Key::Shift,
        Modifier::Win => Key::Meta,
    }
}

fn main_key(key: KeyName) -> Key {
    match key {
        KeyName::Char(c) => Key::Unicode(c),
        KeyName::Named(named) => match named {
            NamedKey::Enter => Key::Return,
            NamedKey::Tab => Key::Tab,
            NamedKey::Escape => Key::Escape,
            NamedKey::Backspace => Key::Backspace,
            NamedKey::Delete => Key::Delete,
            NamedKey::Up => Key::UpArrow,
            NamedKey::Down => Key::DownArrow,
            NamedKey::Left => Key::LeftArrow,
            NamedKey::Right => Key::RightArrow,
            NamedKey::Home => Key::Home,
            NamedKey::End => Key::End,
            NamedKey::PageUp => Key::PageUp,
            NamedKey::PageDown => Key::PageDown,
            NamedKey::Space => Key::Space,
            NamedKey::F(n) => match n {
                1 => Key::F1,
                2 => Key::F2,
                3 => Key::F3,
                4 => Key::F4,
                5 => Key::F5,
                6 => Key::F6,
                7 => Key::F7,
                8 => Key::F8,
                9 => Key::F9,
                10 => Key::F10,
                11 => Key::F11,
                _ => Key::F12,
            },
        },
    }
}
