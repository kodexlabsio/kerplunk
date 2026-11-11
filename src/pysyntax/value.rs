//! The Python value a constant expression folds to: what the readers keep of a name's
//! assignment, a keyword's argument or an f-string's part, with Python's truthiness, `str` and
//! equality, so the folding reads the same as the code it replaces.

use super::text;

/// A folded value. Integers are kept as the decimal text of their digits, as Python holds them
/// without a limit.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    None,
    Bool(bool),
    Int(String),
    Float(f64),
    Imaginary(f64),
    Str(String),
    Bytes(Vec<u8>),
    Ellipsis,
    List(Vec<Value>),
    Dict(Vec<(Value, Value)>),
}

/// Equal keys of a dict hash alike in Python even across types: `1`, `1.0` and `True` are one.
#[derive(PartialEq, Eq, Hash)]
enum Key {
    None,
    Ellipsis,
    Whole(String),
    Small(i128),
    Fraction(u64),
    Imaginary(u64),
    Str(String),
    Bytes(Vec<u8>),
}

impl Value {
    pub fn str(text: impl Into<String>) -> Self {
        Self::Str(text.into())
    }

    /// `{'__asset__': key}`, how the readers mark an asset in a value.
    pub fn marker(name: &str, inner: Value) -> Self {
        Self::Dict(vec![(Self::str(name), inner)])
    }

    /// Python's truth value.
    pub fn truthy(&self) -> bool {
        match self {
            Self::None => false,
            Self::Bool(b) => *b,
            Self::Int(digits) => digits != "0",
            Self::Float(f) | Self::Imaginary(f) => *f != 0.0,
            Self::Str(s) => !s.is_empty(),
            Self::Bytes(b) => !b.is_empty(),
            Self::Ellipsis => true,
            Self::List(items) => !items.is_empty(),
            Self::Dict(items) => !items.is_empty(),
        }
    }

    /// Whether the value holds more than a mebibyte of text or 64 Ki elements, which a file can
    /// build from a few lines (`s = s + s` forty times). A reader leaves such a value unresolved.
    pub fn too_big(&self) -> bool {
        fn spend(value: &Value, bytes: &mut usize, nodes: &mut usize) -> bool {
            let text = match value {
                Value::Str(s) => s.len(),
                Value::Bytes(b) => b.len(),
                _ => 0,
            };
            if text > *bytes || *nodes == 0 {
                return false;
            }
            *bytes -= text;
            *nodes -= 1;
            match value {
                Value::List(items) => items.iter().all(|item| spend(item, bytes, nodes)),
                Value::Dict(items) => items
                    .iter()
                    .all(|(k, v)| spend(k, bytes, nodes) && spend(v, bytes, nodes)),
                _ => true,
            }
        }
        !spend(self, &mut (1 << 20), &mut (1 << 16))
    }

    pub fn is_none(&self) -> bool {
        matches!(self, Self::None)
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_list(&self) -> Option<&[Value]> {
        match self {
            Self::List(items) => Some(items),
            _ => None,
        }
    }

    pub fn as_dict(&self) -> Option<&[(Value, Value)]> {
        match self {
            Self::Dict(items) => Some(items),
            _ => None,
        }
    }

    /// `dict.get(key)` for a string key, which is `None` for anything but a dict.
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.as_dict()?
            .iter()
            .find_map(|(k, v)| (k.as_str() == Some(key)).then_some(v))
    }

    /// `value.get(key)` as the readers test it: present and true.
    pub fn marked(&self, key: &str) -> Option<&Value> {
        self.get(key).filter(|v| v.truthy())
    }

    /// Python's `str(value)`.
    pub fn to_python_str(&self) -> String {
        match self {
            Self::Str(s) => s.clone(),
            other => other.repr(),
        }
    }

    /// Python's `repr(value)`.
    pub fn repr(&self) -> String {
        match self {
            Self::None => "None".into(),
            Self::Bool(true) => "True".into(),
            Self::Bool(false) => "False".into(),
            Self::Int(digits) => digits.clone(),
            Self::Float(f) => text::float_repr(*f),
            Self::Imaginary(f) => text::imaginary_repr(*f),
            Self::Str(s) => text::str_repr(s),
            Self::Bytes(b) => text::bytes_repr(b),
            Self::Ellipsis => "Ellipsis".into(),
            Self::List(items) => {
                let parts: Vec<String> = items.iter().map(Value::repr).collect();
                format!("[{}]", parts.join(", "))
            }
            Self::Dict(items) => {
                let parts: Vec<String> = items
                    .iter()
                    .map(|(k, v)| format!("{}: {}", k.repr(), v.repr()))
                    .collect();
                format!("{{{}}}", parts.join(", "))
            }
        }
    }

    fn key(&self) -> Option<Key> {
        fn number(f: f64) -> Key {
            if f.fract() == 0.0 && f.abs() < 1e30 {
                Key::Small(f as i128)
            } else {
                Key::Fraction(f.to_bits())
            }
        }
        Some(match self {
            Self::None => Key::None,
            Self::Ellipsis => Key::Ellipsis,
            Self::Bool(b) => Key::Small(i128::from(*b)),
            Self::Int(digits) => digits
                .parse::<i128>()
                .map_or_else(|_| Key::Whole(digits.clone()), Key::Small),
            Self::Float(f) => number(*f),
            Self::Imaginary(f) if *f == 0.0 => Key::Small(0),
            Self::Imaginary(f) => Key::Imaginary(f.to_bits()),
            Self::Str(s) => Key::Str(s.clone()),
            Self::Bytes(b) => Key::Bytes(b.clone()),
            // A list or a dict cannot be a key.
            Self::List(_) | Self::Dict(_) => return None,
        })
    }

    /// Builds a dict as a dict display does: a repeated key keeps its first place and takes the
    /// last value. A key that cannot be hashed is left out.
    pub fn dict(pairs: impl IntoIterator<Item = (Value, Value)>) -> Self {
        let mut items: Vec<(Value, Value)> = Vec::new();
        let mut places: std::collections::HashMap<Key, usize> = std::collections::HashMap::new();
        for (key, value) in pairs {
            let Some(wanted) = key.key() else { continue };
            match places.get(&wanted) {
                Some(&place) => items[place].1 = value,
                None => {
                    places.insert(wanted, items.len());
                    items.push((key, value));
                }
            }
        }
        Self::Dict(items)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_repeated_key_keeps_its_place_and_takes_the_last_value() {
        let one = Value::Int("1".into());
        let dict = Value::dict([
            (Value::str("a"), one.clone()),
            (Value::Bool(true), Value::str("x")),
            (Value::str("b"), one.clone()),
            (Value::Float(1.0), Value::str("y")),
        ]);
        assert_eq!(dict.repr(), "{'a': 1, True: 'y', 'b': 1}");
    }

    #[test]
    fn values_over_a_mebibyte_or_64_ki_elements_are_too_big() {
        assert!(!Value::Str("x".repeat(1 << 20)).too_big());
        assert!(Value::Str("x".repeat((1 << 20) + 1)).too_big());
        assert!(!Value::List(vec![Value::None; 1000]).too_big());
        assert!(Value::List(vec![Value::None; 1 << 16]).too_big());
        let nested = Value::List(vec![Value::List(vec![Value::None; 40_000]); 2]);
        assert!(nested.too_big());
    }

    #[test]
    fn values_print_as_python_prints_them() {
        let value = Value::List(vec![
            Value::None,
            Value::Bool(true),
            Value::str("a'b"),
            Value::Float(2.0),
        ]);
        assert_eq!(value.repr(), "[None, True, \"a'b\", 2.0]");
        assert!(!Value::Dict(vec![]).truthy());
        assert!(Value::Ellipsis.truthy());
    }
}
