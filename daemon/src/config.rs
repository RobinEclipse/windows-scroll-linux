//! Validated, bounded configuration shared by the input engine and its loader.
use std::collections::BTreeSet;

pub const MAX_CONFIG_BYTES: usize = 65_536;

#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    pub deadzone_px: f64,
    pub speed_mult: f64,
    pub speed_exp: f64,
    pub max_px_per_sec: f64,
    pub px_per_notch: f64,
    pub max_drag_px: f64,
    pub tick_hz: f64,
    pub natural: bool,
    pub ghost_cursor: bool,
    pub ghost_scale: f64,
    pub snap_cursor_on_release: bool,
    pub extra_devices: Vec<String>,
    pub ignore_devices: Vec<String>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            deadzone_px: 15.0,
            speed_mult: 0.008,
            speed_exp: 2.2,
            max_px_per_sec: 30_000.0,
            px_per_notch: 55.0,
            max_drag_px: 1_200.0,
            tick_hz: 90.0,
            natural: false,
            ghost_cursor: true,
            ghost_scale: 1.0,
            snap_cursor_on_release: true,
            extra_devices: Vec::new(),
            ignore_devices: Vec::new(),
        }
    }
}

fn bounded(name: &str, value: f64, low: f64, high: f64) -> Result<(), String> {
    if !value.is_finite() || value < low || value > high {
        return Err(format!(
            "{name} must be a finite number between {low} and {high}"
        ));
    }
    Ok(())
}

fn boolean(value: &str) -> Result<bool, String> {
    match value.to_ascii_lowercase().as_str() {
        "true" | "yes" | "on" | "1" => Ok(true),
        "false" | "no" | "off" | "0" => Ok(false),
        _ => Err("expected true or false".into()),
    }
}

fn validate_devices(values: &[String]) -> Result<(), String> {
    if values.len() > 32 {
        return Err("at most 32 device selectors are allowed per list".into());
    }
    for value in values {
        if value.len() > 128 || value.trim() != value || value.chars().any(char::is_control) {
            return Err("device selectors must be at most 128 bytes without control characters or surrounding whitespace".into());
        }
        if value.starts_with('/') {
            if !value.starts_with("/dev/input/")
                || value == "/dev/input/"
                || value.split('/').any(|part| part == ".." || part == ".")
            {
                return Err(
                    "device paths must name a node below /dev/input without . or .. components"
                        .into(),
                );
            }
        } else {
            let id = value.split_once(':').is_some_and(|(vendor, product)| {
                [vendor, product].iter().all(|part| {
                    !part.is_empty()
                        && part.len() <= 4
                        && part.bytes().all(|b| b.is_ascii_hexdigit())
                })
            });
            if !id && value.chars().count() < 3 {
                return Err("device name selectors must contain at least three characters".into());
            }
        }
    }
    Ok(())
}

fn devices(value: &str) -> Result<Vec<String>, String> {
    let values: Vec<_> = value
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect();
    validate_devices(&values)?;
    Ok(values)
}

impl Config {
    pub fn parse(text: &str) -> Result<Self, String> {
        parse(text)
    }

    pub fn validate(&self) -> Result<(), String> {
        bounded("DEADZONE_PX", self.deadzone_px, 0.0, 1_000.0)?;
        bounded("SPEED_MULT", self.speed_mult, 0.000_001, 1_000.0)?;
        bounded("SPEED_EXP", self.speed_exp, 0.1, 8.0)?;
        bounded("MAX_PX_PER_SEC", self.max_px_per_sec, 1.0, 100_000.0)?;
        bounded("PX_PER_NOTCH", self.px_per_notch, 1.0, 10_000.0)?;
        bounded("MAX_DRAG_PX", self.max_drag_px, 1.0, 10_000.0)?;
        bounded("TICK_HZ", self.tick_hz, 10.0, 500.0)?;
        bounded("GHOST_SCALE", self.ghost_scale, 0.1, 10.0)?;
        if self.max_drag_px <= self.deadzone_px {
            return Err("MAX_DRAG_PX must be greater than DEADZONE_PX".into());
        }
        validate_devices(&self.extra_devices)?;
        validate_devices(&self.ignore_devices)?;
        Ok(())
    }
}

/// Parse legacy KEY=value files, preserving only the supported behavior.
/// Obsolete passthrough options are accepted for upgrade compatibility but
/// cannot change the no-paste policy. A malformed file is rejected in full.
pub fn parse(text: &str) -> Result<Config, String> {
    if text.len() > MAX_CONFIG_BYTES {
        return Err("configuration exceeds 64 KiB".into());
    }
    let mut config = Config::default();
    let mut seen = BTreeSet::new();
    for (index, raw) in text.lines().enumerate() {
        let line = raw.split('#').next().unwrap_or("").trim();
        if line.is_empty() {
            continue;
        }
        let (key, value) = line
            .split_once('=')
            .ok_or_else(|| format!("line {}: expected KEY=value", index + 1))?;
        let key = key.trim();
        let value = value.trim();
        if !seen.insert(key) {
            return Err(format!("line {}: duplicate key {key}", index + 1));
        }
        let number = || {
            value
                .parse::<f64>()
                .map_err(|_| format!("line {}: {key} requires a number", index + 1))
        };
        let result: Result<(), String> = (|| {
            match key {
                "DEADZONE_PX" => config.deadzone_px = number()?,
                "SPEED_MULT" => config.speed_mult = number()?,
                "SPEED_EXP" => config.speed_exp = number()?,
                "MAX_PX_PER_SEC" => config.max_px_per_sec = number()?,
                "PX_PER_NOTCH" => config.px_per_notch = number()?,
                "MAX_DRAG_PX" => config.max_drag_px = number()?,
                "TICK_HZ" => config.tick_hz = number()?,
                "GHOST_SCALE" => config.ghost_scale = number()?,
                "NATURAL" => config.natural = boolean(value)?,
                "GHOST_CURSOR" => config.ghost_cursor = boolean(value)?,
                "SNAP_CURSOR_ON_RELEASE" => config.snap_cursor_on_release = boolean(value)?,
                "EXTRA_DEVICES" => config.extra_devices = devices(value)?,
                "IGNORE_DEVICES" => config.ignore_devices = devices(value)?,
                "TOGGLE_MODE" | "DESKTOP_SCROLL" => {
                    boolean(value)?;
                }
                "BLACKLIST" => { /* Legacy native-app bypass is intentionally unsupported. */ }
                "ALLOW_KEYBOARDS" => {
                    if boolean(value)? {
                        return Err("keyboard interception is not supported".into());
                    }
                }
                _ => return Err(format!("unknown configuration key {key}")),
            }
            Ok(())
        })();
        result.map_err(|e| format!("line {}: {e}", index + 1))?;
    }
    config.validate()?;
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_valid() {
        Config::default().validate().unwrap();
        assert_eq!(parse("").unwrap(), Config::default());
    }
    #[test]
    fn legacy_comments_and_options() {
        let c = parse("# settings\nTICK_HZ = 120 # Hz\nNATURAL = YeS\nTOGGLE_MODE=false\nBLACKLIST=everything\nDESKTOP_SCROLL=true\nIGNORE_DEVICES= CORSAIR STRAFE, 1234:abcd, /dev/input/by-id/mouse\n").unwrap();
        assert_eq!(c.tick_hz, 120.0);
        assert!(c.natural);
        assert_eq!(c.ignore_devices.len(), 3);
    }
    #[test]
    fn invalid_and_nonfinite_numbers_rejected() {
        for text in [
            "SPEED_EXP=1000",
            "SPEED_EXP=NaN",
            "SPEED_MULT=inf",
            "PX_PER_NOTCH=0",
            "TICK_HZ=0",
            "GHOST_SCALE=-1",
            "MAX_DRAG_PX=15",
            "DEADZONE_PX=-1",
        ] {
            assert!(parse(text).is_err(), "{text}");
        }
    }
    #[test]
    fn invalid_syntax_rejected() {
        for text in [
            "garbage",
            "MISPLED=1",
            "NATURAL=maybe",
            "NATURAL=true\nNATURAL=false",
            "ALLOW_KEYBOARDS=true",
        ] {
            assert!(parse(text).is_err(), "{text}");
        }
    }
    #[test]
    fn device_selectors_are_bounded() {
        for text in [
            "EXTRA_DEVICES=/etc/shadow",
            "IGNORE_DEVICES=/dev/input/../x",
            "EXTRA_DEVICES=ab",
            "IGNORE_DEVICES=/dev/input/",
            "IGNORE_DEVICES=Mouse\0Suffix",
        ] {
            assert!(parse(text).is_err(), "{text}");
        }
        assert!(parse(&format!("EXTRA_DEVICES={}", "x".repeat(129))).is_err());
        assert!(parse(&format!("EXTRA_DEVICES={}", ["mouse"; 33].join(","))).is_err());
        assert!(parse(&"#".repeat(MAX_CONFIG_BYTES + 1)).is_err());
    }
    #[test]
    fn maximum_configuration_is_finite() {
        let c = parse("DEADZONE_PX=1000\nMAX_DRAG_PX=10000\nSPEED_MULT=1000\nSPEED_EXP=8\nMAX_PX_PER_SEC=100000\nPX_PER_NOTCH=1\nTICK_HZ=500\nGHOST_SCALE=10").unwrap();
        c.validate().unwrap();
    }
}
