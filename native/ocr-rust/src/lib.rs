use image::{Rgb, RgbImage, imageops};
use ort::{
    environment::Environment,
    session::{Session, builder::GraphOptimizationLevel},
    value::Tensor,
};
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    prelude::*,
};
use regex::Regex;
use std::{
    path::Path,
    sync::{LazyLock, Mutex},
};

type Failure = Box<dyn std::error::Error + Send + Sync>;
type Value = (u64, u16);
type LineResult = (Option<(u64, f64)>, f64, String, Vec<String>);
const MODEL_HEIGHT: usize = 48;

struct Model {
    session: Session,
    alphabet: Vec<String>,
}

impl Model {
    fn load(environment: &Environment, path: &Path) -> Result<Self, Failure> {
        let session = Session::builder(environment)?
            .with_optimization_level(GraphOptimizationLevel::All)
            .map_err(|error| error.to_string())?
            .with_intra_threads(2)
            .map_err(|error| error.to_string())?
            .with_inter_threads(1)
            .map_err(|error| error.to_string())?
            .with_intra_op_spinning(false)
            .map_err(|error| error.to_string())?
            .commit_from_file(path)?;
        let dictionary = session
            .metadata()?
            .custom("character")
            .ok_or("missing model alphabet")?;
        let alphabet = std::iter::once(String::new())
            .chain(dictionary.lines().map(str::to_owned))
            .chain(std::iter::once(" ".into()))
            .collect();
        Ok(Self { session, alphabet })
    }

    fn read(&mut self, image: &RgbImage, minimum_width: usize) -> Result<(String, f64), Failure> {
        let ratio_width =
            MODEL_HEIGHT as f64 * f64::from(image.width()) / f64::from(image.height());
        let width = (ratio_width.ceil() as usize)
            .max(minimum_width)
            .next_multiple_of(4);
        let resized_width = ratio_width.ceil() as usize;
        if width > 8192 {
            return Err("OCR input is too wide".into());
        }
        let resized = imageops::resize(
            image,
            resized_width as u32,
            MODEL_HEIGHT as u32,
            imageops::FilterType::Triangle,
        );
        let plane = MODEL_HEIGHT * width;
        let mut input = vec![0.0_f32; 3 * plane];
        for (x, y, rgb) in resized.enumerate_pixels() {
            let offset = y as usize * width + x as usize;
            for channel in 0..3 {
                input[channel * plane + offset] = f32::from(rgb[2 - channel]) / 127.5 - 1.0;
            }
        }
        let tensor = Tensor::from_array(([1, 3, MODEL_HEIGHT, width], input.into_boxed_slice()))?;
        let output = self.session.run(ort::inputs![tensor])?;
        let (shape, probabilities) = output[0].try_extract_tensor::<f32>()?;
        if shape.len() != 3 || shape[0] != 1 || shape[2] != self.alphabet.len() as i64 {
            return Err("unexpected OCR output dimensions".into());
        }
        Ok(decode(probabilities, &self.alphabet))
    }
}

fn decode(probabilities: &[f32], alphabet: &[String]) -> (String, f64) {
    let mut previous = 0;
    let mut sum = 0.0;
    let mut count = 0;
    let mut text = String::new();
    for step in probabilities.chunks_exact(alphabet.len()) {
        let mut best = 0;
        for (index, &score) in step.iter().enumerate().skip(1) {
            if score > step[best] {
                best = index;
            }
        }
        if best != previous && best != 0 {
            text.push_str(&alphabet[best]);
            sum += (f64::from(step[best]) * 100_000.0).round() / 100_000.0;
            count += 1;
        }
        previous = best;
    }
    let confidence = if count == 0 {
        0.0
    } else {
        (sum * 100_000.0 / f64::from(count)).round() / 100_000.0
    };
    (text, confidence)
}

static EXP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)^\s*(?:(EXP[.:]?)\s*)?((?:[0-9]{1,3}(?:,[0-9]{3})+)|[0-9]{1,12})\s*([\[({])\s*([0-9]{1,2})[.,]([0-9]{2})\s*([%％xX×]?)\s*([\])}]{0,3})\s*$").unwrap()
});
static LABEL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^EXP[.:]?\s*[0-9]").unwrap());

fn parse(raw: &str) -> Option<Value> {
    let fields = EXP.captures(raw)?;
    let percent = &fields[6];
    if fields[7].is_empty() && (fields.get(1).is_none() || !matches!(percent, "%" | "％")) {
        return None;
    }
    let digits = fields[2].replace(',', "");
    if digits.len() > 12 {
        return None;
    }
    let exp = digits.parse().ok()?;
    let integer = fields[4].parse::<u16>().ok()?;
    let decimal = fields[5].parse::<u16>().ok()?;
    Some((exp, integer * 100 + decimal))
}

#[derive(Clone, Copy)]
enum Filter {
    Color,
    Gray,
    Neutral,
}

fn enhance(image: &RgbImage, filter: Filter) -> RgbImage {
    if matches!(filter, Filter::Color) {
        return image.clone();
    }
    let mut pixels = Vec::with_capacity((image.width() * image.height()) as usize);
    let (mut low, mut high) = (255, 0);
    for &Rgb([r, g, b]) in image.pixels() {
        let value = match filter {
            Filter::Color => unreachable!(),
            Filter::Gray => {
                ((u32::from(r) * 299 + u32::from(g) * 587 + u32::from(b) * 114 + 500) / 1000) as u8
            }
            Filter::Neutral => r.min(g).min(b),
        };
        low = low.min(value);
        high = high.max(value);
        pixels.push(value);
    }
    RgbImage::from_fn(image.width(), image.height(), |x, y| {
        let value = pixels[(y * image.width() + x) as usize];
        let mapped = if high == low {
            value
        } else {
            (u16::from(value - low) * 255 / u16::from(high - low)) as u8
        };
        Rgb([mapped; 3])
    })
}

fn prepare(image: &RgbImage, filter: Filter, scale: u32) -> RgbImage {
    let enlarged = imageops::resize(
        &enhance(image, filter),
        image.width() * scale,
        image.height() * scale,
        imageops::FilterType::CatmullRom,
    );
    let margin = scale * 5;
    let mut padded = RgbImage::new(
        enlarged.width() + 2 * margin,
        enlarged.height() + 2 * margin,
    );
    imageops::replace(&mut padded, &enlarged, i64::from(margin), i64::from(margin));
    padded
}

struct Reading {
    raw: String,
    confidence: f64,
    value: Option<Value>,
}

fn read_variant(
    model: &mut Model,
    image: &RgbImage,
    filter: Filter,
    scale: u32,
    minimum_width: usize,
) -> Result<Reading, Failure> {
    let (raw, confidence) = model.read(&prepare(image, filter, scale), minimum_width)?;
    let value = parse(&raw);
    Ok(Reading {
        raw,
        confidence,
        value,
    })
}

fn consistent((exp, basis_points): Value, total: u64) -> bool {
    exp < total
        && (u128::from(exp) * 10_000).abs_diff(u128::from(basis_points) * u128::from(total))
            <= 2 * u128::from(total)
}

fn pair(readings: &[Reading], threshold: f64, total: Option<u64>) -> Option<(Value, f64, String)> {
    for (index, first) in readings.iter().enumerate() {
        let Some(value) = first.value else { continue };
        if first.confidence < threshold || total.is_some_and(|total| !consistent(value, total)) {
            continue;
        }
        if let Some(second) = readings[index + 1..]
            .iter()
            .find(|other| other.value == Some(value) && other.confidence >= threshold)
        {
            return Some((
                value,
                first.confidence.min(second.confidence),
                first.raw.clone(),
            ));
        }
    }
    None
}

// A missing or doubled bracket can corroborate a strict reading from
// another model. It can never supply a value on its own.
static SUPPORT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)^\s*EXP[.:]?\s*([0-9]{1,12})(?:\s*[\[({]{1,2}\s*|\s+)([0-9]{1,2})[.,]([0-9]{2})\s*[%％xX×]\s*[\])}]{0,3}\s*$").unwrap()
});

fn supporting_value(raw: &str) -> Option<Value> {
    let fields = SUPPORT.captures(raw)?;
    let exp = fields[1].parse().ok()?;
    let integer = fields[2].parse::<u16>().ok()?;
    let decimal = fields[3].parse::<u16>().ok()?;
    Some((exp, integer * 100 + decimal))
}

fn cross_model(medium: &Reading, fast: &[Reading], total: u64) -> Option<(Value, f64, String)> {
    let value = medium.value.or_else(|| supporting_value(&medium.raw))?;
    if medium.confidence < 0.88 || !consistent(value, total) {
        return None;
    }
    for entry in fast {
        if entry.confidence < 0.88 || (medium.value.is_none() && entry.value.is_none()) {
            continue;
        }
        if entry.value.or_else(|| supporting_value(&entry.raw)) == Some(value) {
            let raw = if medium.value.is_some() {
                &medium.raw
            } else {
                &entry.raw
            };
            return Some((value, medium.confidence.min(entry.confidence), raw.clone()));
        }
    }
    None
}

struct Models {
    medium: Model,
    fast: Option<Model>,
}

impl Models {
    fn read_exp(&mut self, image: &RgbImage, total: Option<u64>) -> Result<LineResult, Failure> {
        let mut fast_readings = Vec::new();
        if let (Some(fast), Some(total)) = (&mut self.fast, total) {
            let color = read_variant(fast, image, Filter::Color, 2, 0)?;
            let (second, third) = contrast_order(&color.raw);
            fast_readings.push(color);
            fast_readings.push(read_variant(fast, image, second, 2, 0)?);
            if let Some(accepted) = pair(&fast_readings, 0.92, Some(total)) {
                return Ok(format_result(Some(accepted), fast_readings));
            }
            fast_readings.push(read_variant(fast, image, third, 2, 0)?);
            if let Some(accepted) = pair(&fast_readings, 0.92, Some(total)) {
                return Ok(format_result(Some(accepted), fast_readings));
            }
        }

        let mut readings = Vec::new();
        let minimum_width = if fast_readings.is_empty() { 320 } else { 0 };
        let color = read_variant(&mut self.medium, image, Filter::Color, 4, minimum_width)?;
        let (second, third) = contrast_order(&color.raw);
        readings.push(color);
        if !fast_readings.is_empty()
            && readings
                .iter()
                .chain(&fast_readings)
                .all(|entry| !contains_digits(&entry.raw))
        {
            readings.extend(fast_readings);
            return Ok(format_result(None, readings));
        }
        for filter in [Some(second), Some(third), None] {
            let accepted = pair(&readings, 0.85, total).or_else(|| {
                total.and_then(|total| {
                    readings
                        .iter()
                        .find_map(|entry| cross_model(entry, &fast_readings, total))
                })
            });
            if accepted.is_some() {
                readings.extend(fast_readings);
                return Ok(format_result(accepted, readings));
            }
            if let Some(filter) = filter {
                readings.push(read_variant(
                    &mut self.medium,
                    image,
                    filter,
                    4,
                    minimum_width,
                )?);
            }
        }
        if let Some(total) = total.filter(|_| !fast_readings.is_empty()) {
            let extra = read_variant(&mut self.medium, image, Filter::Gray, 2, 0)?;
            let accepted = cross_model(&extra, &fast_readings, total);
            readings.push(extra);
            if accepted.is_some() {
                readings.extend(fast_readings);
                return Ok(format_result(accepted, readings));
            }

            // Some short glyphs are clipped by the compact input tensor.
            // Retry at the legacy width if any view saw plausible EXP fields.
            let plausible = readings.iter().chain(&fast_readings).any(|entry| {
                entry
                    .value
                    .or_else(|| supporting_value(&entry.raw))
                    .is_some_and(|value| consistent(value, total))
            });
            let padded_width = (48.0 * f64::from(image.width() + 10)
                / f64::from(image.height() + 10))
            .ceil() as usize;
            if plausible && padded_width.next_multiple_of(4) < 320 {
                let mut wide = Vec::new();
                for filter in [Filter::Color, second, third] {
                    wide.push(read_variant(&mut self.medium, image, filter, 4, 320)?);
                    let accepted = pair(&wide, 0.85, Some(total))
                        .or_else(|| cross_model(wide.last().unwrap(), &fast_readings, total));
                    if accepted.is_some() {
                        readings.extend(wide);
                        readings.extend(fast_readings);
                        return Ok(format_result(accepted, readings));
                    }
                }
                readings.extend(wide);
            }
        }
        readings.extend(fast_readings);
        Ok(format_result(None, readings))
    }
}

fn contains_digits(raw: &str) -> bool {
    raw.as_bytes().iter().any(u8::is_ascii_digit)
}

fn contrast_order(raw: &str) -> (Filter, Filter) {
    if LABEL.is_match(raw) {
        (Filter::Gray, Filter::Neutral)
    } else {
        (Filter::Neutral, Filter::Gray)
    }
}

fn format_result(accepted: Option<(Value, f64, String)>, readings: Vec<Reading>) -> LineResult {
    let variants = readings.iter().map(|entry| entry.raw.clone()).collect();
    if let Some(((exp, percent), confidence, raw)) = accepted {
        return (
            Some((exp, f64::from(percent) / 100.0)),
            confidence,
            raw,
            variants,
        );
    }
    (
        None,
        readings[0].confidence,
        readings[0].raw.clone(),
        variants,
    )
}

#[pyclass]
struct Recognizer {
    models: Mutex<Models>,
}

#[pymethods]
impl Recognizer {
    #[new]
    fn new(runtime: &str, medium: &str, fast: Option<&str>) -> PyResult<Self> {
        let environment = ort::init_from(runtime)
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?
            .with_telemetry(false)
            .build()
            .map_err(|error| runtime_error(error.into()))?;
        let medium = Model::load(&environment, Path::new(medium)).map_err(runtime_error)?;
        let fast = fast
            .map(|file| Model::load(&environment, Path::new(file)))
            .transpose()
            .map_err(runtime_error)?;
        Ok(Self {
            models: Mutex::new(Models { medium, fast }),
        })
    }

    fn text(&self, py: Python<'_>, rgb: &[u8], width: u32, height: u32) -> PyResult<(String, f64)> {
        let image = from_rgb(rgb, width, height)?;
        py.detach(|| {
            self.models
                .lock()
                .map_err(|_| "OCR session poisoned".into())
                .and_then(|mut model| model.medium.read(&image, 96))
        })
        .map_err(runtime_error)
    }

    fn experience(
        &self,
        py: Python<'_>,
        rgb: &[u8],
        width: u32,
        height: u32,
        total: Option<u64>,
    ) -> PyResult<LineResult> {
        let image = from_rgb(rgb, width, height)?;
        py.detach(|| {
            self.models
                .lock()
                .map_err(|_| "OCR session poisoned".into())
                .and_then(|mut model| model.read_exp(&image, total))
        })
        .map_err(runtime_error)
    }
}

fn from_rgb(rgb: &[u8], width: u32, height: u32) -> PyResult<RgbImage> {
    if width == 0
        || height == 0
        || width > 4096
        || height > 1024
        || rgb.len() != width as usize * height as usize * 3
    {
        return Err(PyValueError::new_err("invalid RGB dimensions"));
    }
    Ok(RgbImage::from_raw(width, height, rgb.to_vec()).unwrap())
}

fn runtime_error(error: Failure) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

#[pymodule]
fn _planet_ocr(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Recognizer>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_hud_without_inventing_punctuation_or_digits() {
        assert_eq!(parse("EXP. 285072805 [56.75%"), Some((285072805, 5675)));
        assert_eq!(parse("188,784,387[41,81X]"), Some((188784387, 4181)));
        assert_eq!(parse("188784387[41.81%"), None);
        assert_eq!(parse("EXP 188 784387 [41.81%]"), None);
        assert_eq!(parse("EXP 188784387 [100.00%]"), None);
    }

    #[test]
    fn rejects_agreement_at_wrong_percentage() {
        let votes = [
            Reading {
                raw: "EXP 1234 [10.00%]".into(),
                confidence: 0.97,
                value: Some((1234, 1000)),
            },
            Reading {
                raw: "EXP 1234 [10.00%]".into(),
                confidence: 0.97,
                value: Some((1234, 1000)),
            },
        ];
        assert!(pair(&votes, 0.92, Some(12_340)).is_some());
        assert!(pair(&votes, 0.92, Some(11_000)).is_none());
    }

    #[test]
    fn weak_punctuation_only_corroborates_a_strict_reading() {
        let weak = Reading {
            raw: "EXP 1234 10.00×".into(),
            confidence: 0.95,
            value: None,
        };
        let strict = Reading {
            raw: "EXP 1234 [10.00%]".into(),
            confidence: 0.96,
            value: Some((1234, 1000)),
        };
        assert!(cross_model(&weak, std::slice::from_ref(&weak), 12_340).is_none());
        assert!(cross_model(&strict, std::slice::from_ref(&weak), 12_340).is_some());
        assert!(cross_model(&strict, std::slice::from_ref(&weak), 11_000).is_none());
        assert_eq!(supporting_value("123410.00%"), None);
    }

    #[test]
    fn decodes_ctc_blank_and_duplicate_tokens() {
        let chars = vec!["".to_owned(), "1".to_owned(), "2".to_owned()];
        let steps = [
            0.0, 0.9, 0.1, 0.0, 0.8, 0.2, 0.9, 0.1, 0.0, 0.0, 0.9, 0.1, 0.0, 0.1, 0.9,
        ];
        assert_eq!(decode(&steps, &chars), ("112".to_owned(), 0.9));
    }
}
