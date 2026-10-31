//! Screen capture and pointer/keyboard input on the user's machine.

use base64::{engine::general_purpose::STANDARD, Engine as _};
use enigo::{Coordinate, Enigo, Keyboard, Mouse, Settings};
use png::{BitDepth, ColorType, Encoder};
use screenshots::Screen;

pub fn screenshot_png_base64() -> Result<String, String> {
    let screen = Screen::all()
        .map_err(|err| err.to_string())?
        .into_iter()
        .next()
        .ok_or_else(|| "no_monitor".to_string())?;
    let shot = screen.capture().map_err(|err| err.to_string())?;
    let width = shot.width();
    let height = shot.height();
    let mut buf = Vec::new();
    {
        let mut encoder = Encoder::new(&mut buf, width, height);
        encoder.set_color(ColorType::Rgba);
        encoder.set_depth(BitDepth::Eight);
        let mut writer = encoder.write_header().map_err(|err| err.to_string())?;
        writer
            .write_image_data(shot.rgba())
            .map_err(|err| err.to_string())?;
    }
    Ok(STANDARD.encode(buf))
}

pub fn pointer_click(x: i32, y: i32) -> Result<(), String> {
    let mut enigo = Enigo::new(&Settings::default()).map_err(|err| err.to_string())?;
    enigo
        .move_mouse(x, y, Coordinate::Abs)
        .map_err(|err| err.to_string())?;
    enigo
        .button(enigo::Button::Left, enigo::Direction::Click)
        .map_err(|err| err.to_string())
}

pub fn type_text(text: &str) -> Result<(), String> {
    let mut enigo = Enigo::new(&Settings::default()).map_err(|err| err.to_string())?;
    enigo.text(text).map_err(|err| err.to_string())
}
