use std::collections::BTreeMap;

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

use crate::schema::{EncodingVersion, PropertyId, SchemaCatalog, SchemaEpoch};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PropertyPhysicalEncoding {
    PlainI32,
    PlainI64,
    PlainF64,
}

impl PropertyPhysicalEncoding {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "plain_i32" => Ok(Self::PlainI32),
            "plain_i64" => Ok(Self::PlainI64),
            "plain_f64" => Ok(Self::PlainF64),
            _ => bail!("unsupported property physical encoding {value}"),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum PropertyValue {
    I32(i32),
    I64(i64),
    F64(f64),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct PropertyEncodingSpec {
    pub property_id: PropertyId,
    pub encoding_epoch: SchemaEpoch,
    pub physical_encoding: PropertyPhysicalEncoding,
    pub encoding_version: EncodingVersion,
}

#[derive(Debug, Default, Clone)]
pub struct PropertyEncodingRegistry {
    specs: BTreeMap<(PropertyId, SchemaEpoch), PropertyEncodingSpec>,
}

impl PropertyEncodingRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn from_schema_catalog(catalog: &SchemaCatalog) -> Result<Self> {
        let mut registry = Self::new();
        for entry in &catalog.property_encoding_history {
            registry.register(PropertyEncodingSpec {
                property_id: entry.property_id,
                encoding_epoch: entry.encoding_epoch,
                physical_encoding: PropertyPhysicalEncoding::parse(&entry.physical_encoding)?,
                encoding_version: entry.encoding_version,
            });
        }
        for entry in catalog.properties.values() {
            if entry.alias_of.is_none() && registry.spec(entry.id, entry.encoding_epoch).is_none() {
                registry.register(PropertyEncodingSpec {
                    property_id: entry.id,
                    encoding_epoch: entry.encoding_epoch,
                    physical_encoding: PropertyPhysicalEncoding::parse(&entry.physical_encoding)?,
                    encoding_version: entry.encoding_version,
                });
            }
        }
        Ok(registry)
    }

    pub fn register(&mut self, spec: PropertyEncodingSpec) {
        self.specs
            .insert((spec.property_id, spec.encoding_epoch), spec);
    }

    pub fn spec(
        &self,
        property_id: PropertyId,
        encoding_epoch: SchemaEpoch,
    ) -> Option<PropertyEncodingSpec> {
        self.specs.get(&(property_id, encoding_epoch)).copied()
    }

    pub fn decode(
        &self,
        property_id: PropertyId,
        encoding_epoch: SchemaEpoch,
        bytes: &[u8],
    ) -> Result<PropertyValue> {
        let Some(spec) = self.spec(property_id, encoding_epoch) else {
            bail!("missing decoder for property_id={property_id} encoding_epoch={encoding_epoch}");
        };
        decode_with_encoding(spec.physical_encoding, bytes)
    }
}

pub fn encode_with_encoding(
    physical_encoding: PropertyPhysicalEncoding,
    value: &PropertyValue,
) -> Result<Vec<u8>> {
    match (physical_encoding, value) {
        (PropertyPhysicalEncoding::PlainI32, PropertyValue::I32(value)) => {
            Ok(value.to_le_bytes().to_vec())
        }
        (PropertyPhysicalEncoding::PlainI64, PropertyValue::I64(value)) => {
            Ok(value.to_le_bytes().to_vec())
        }
        (PropertyPhysicalEncoding::PlainF64, PropertyValue::F64(value)) => {
            Ok(value.to_le_bytes().to_vec())
        }
        (PropertyPhysicalEncoding::PlainI32, _) => bail!("plain_i32 expects PropertyValue::I32"),
        (PropertyPhysicalEncoding::PlainI64, _) => bail!("plain_i64 expects PropertyValue::I64"),
        (PropertyPhysicalEncoding::PlainF64, _) => bail!("plain_f64 expects PropertyValue::F64"),
    }
}

pub fn parse_default_or_null_rule(
    physical_encoding: PropertyPhysicalEncoding,
    default_or_null_rule: &str,
) -> Result<Option<PropertyValue>> {
    let rule = default_or_null_rule.trim();
    if rule.is_empty() || rule.eq_ignore_ascii_case("null") || rule.eq_ignore_ascii_case("none") {
        return Ok(None);
    }
    let Some(literal) = rule.strip_prefix("default:") else {
        bail!("unsupported default_or_null_rule {default_or_null_rule}");
    };
    match physical_encoding {
        PropertyPhysicalEncoding::PlainI32 => literal
            .parse::<i32>()
            .map(PropertyValue::I32)
            .map(Some)
            .map_err(Into::into),
        PropertyPhysicalEncoding::PlainI64 => literal
            .parse::<i64>()
            .map(PropertyValue::I64)
            .map(Some)
            .map_err(Into::into),
        PropertyPhysicalEncoding::PlainF64 => literal
            .parse::<f64>()
            .map(PropertyValue::F64)
            .map(Some)
            .map_err(Into::into),
    }
}

pub fn decode_with_encoding(
    physical_encoding: PropertyPhysicalEncoding,
    bytes: &[u8],
) -> Result<PropertyValue> {
    match physical_encoding {
        PropertyPhysicalEncoding::PlainI32 => {
            if bytes.len() != 4 {
                bail!("plain_i32 expects 4 bytes, got {}", bytes.len());
            }
            Ok(PropertyValue::I32(i32::from_le_bytes(
                bytes.try_into().expect("length checked"),
            )))
        }
        PropertyPhysicalEncoding::PlainI64 => {
            if bytes.len() != 8 {
                bail!("plain_i64 expects 8 bytes, got {}", bytes.len());
            }
            Ok(PropertyValue::I64(i64::from_le_bytes(
                bytes.try_into().expect("length checked"),
            )))
        }
        PropertyPhysicalEncoding::PlainF64 => {
            if bytes.len() != 8 {
                bail!("plain_f64 expects 8 bytes, got {}", bytes.len());
            }
            Ok(PropertyValue::F64(f64::from_le_bytes(
                bytes.try_into().expect("length checked"),
            )))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::{NewPropertyEntry, PropertyOwner, SchemaCatalog};

    #[test]
    fn property_encoding_registry_dispatches_by_epoch() {
        let mut registry = PropertyEncodingRegistry::new();
        registry.register(PropertyEncodingSpec {
            property_id: 5,
            encoding_epoch: 1,
            physical_encoding: PropertyPhysicalEncoding::PlainI64,
            encoding_version: 1,
        });
        registry.register(PropertyEncodingSpec {
            property_id: 5,
            encoding_epoch: 2,
            physical_encoding: PropertyPhysicalEncoding::PlainF64,
            encoding_version: 2,
        });

        assert_eq!(
            registry.decode(5, 1, &42_i64.to_le_bytes()).unwrap(),
            PropertyValue::I64(42)
        );
        assert_eq!(
            registry.decode(5, 2, &42.5_f64.to_le_bytes()).unwrap(),
            PropertyValue::F64(42.5)
        );
    }

    #[test]
    fn property_encoding_registry_rejects_missing_epoch() {
        let registry = PropertyEncodingRegistry::new();
        let err = registry
            .decode(5, 7, &42_i64.to_le_bytes())
            .expect_err("missing decoder should fail");
        assert!(
            err.to_string().contains("missing decoder"),
            "unexpected error: {err:#}"
        );
    }

    #[test]
    fn property_decoder_checks_fixed_width() {
        let err = decode_with_encoding(PropertyPhysicalEncoding::PlainI64, &[1, 2, 3])
            .expect_err("wrong width should fail");
        assert!(
            err.to_string().contains("expects 8 bytes"),
            "unexpected error: {err:#}"
        );
    }

    #[test]
    fn property_encoder_round_trips_fixed_width_values() {
        assert_eq!(
            decode_with_encoding(
                PropertyPhysicalEncoding::PlainI32,
                &encode_with_encoding(PropertyPhysicalEncoding::PlainI32, &PropertyValue::I32(7))
                    .unwrap(),
            )
            .unwrap(),
            PropertyValue::I32(7)
        );
        assert_eq!(
            decode_with_encoding(
                PropertyPhysicalEncoding::PlainI64,
                &encode_with_encoding(PropertyPhysicalEncoding::PlainI64, &PropertyValue::I64(42))
                    .unwrap(),
            )
            .unwrap(),
            PropertyValue::I64(42)
        );
        assert_eq!(
            decode_with_encoding(
                PropertyPhysicalEncoding::PlainF64,
                &encode_with_encoding(
                    PropertyPhysicalEncoding::PlainF64,
                    &PropertyValue::F64(7.25),
                )
                .unwrap(),
            )
            .unwrap(),
            PropertyValue::F64(7.25)
        );
    }

    #[test]
    fn property_encoder_rejects_type_mismatch() {
        let err =
            encode_with_encoding(PropertyPhysicalEncoding::PlainI64, &PropertyValue::F64(1.0))
                .expect_err("wrong logical value should fail");
        assert!(
            err.to_string().contains("plain_i64 expects"),
            "unexpected error: {err:#}"
        );
    }

    #[test]
    fn property_default_rule_parses_null_and_fixed_width_defaults() {
        assert_eq!(
            parse_default_or_null_rule(PropertyPhysicalEncoding::PlainI64, "null").unwrap(),
            None
        );
        assert_eq!(
            parse_default_or_null_rule(PropertyPhysicalEncoding::PlainI32, "default:7").unwrap(),
            Some(PropertyValue::I32(7))
        );
        assert_eq!(
            parse_default_or_null_rule(PropertyPhysicalEncoding::PlainI64, "default:42").unwrap(),
            Some(PropertyValue::I64(42))
        );
        assert_eq!(
            parse_default_or_null_rule(PropertyPhysicalEncoding::PlainF64, "default:7.25").unwrap(),
            Some(PropertyValue::F64(7.25))
        );
    }

    #[test]
    fn property_default_rule_rejects_unsupported_rule() {
        let err = parse_default_or_null_rule(PropertyPhysicalEncoding::PlainI64, "zero")
            .expect_err("unsupported default rule should fail");
        assert!(
            err.to_string().contains("unsupported default_or_null_rule"),
            "unexpected error: {err:#}"
        );
    }

    #[test]
    fn property_physical_encoding_parses_catalog_names() {
        assert_eq!(
            PropertyPhysicalEncoding::parse("plain_i32").unwrap(),
            PropertyPhysicalEncoding::PlainI32
        );
        assert_eq!(
            PropertyPhysicalEncoding::parse("plain_i64").unwrap(),
            PropertyPhysicalEncoding::PlainI64
        );
        assert_eq!(
            PropertyPhysicalEncoding::parse("plain_f64").unwrap(),
            PropertyPhysicalEncoding::PlainF64
        );
        assert!(PropertyPhysicalEncoding::parse("varint").is_err());
    }

    #[test]
    fn property_encoding_registry_builds_from_catalog_history() {
        let mut catalog = SchemaCatalog::new();
        catalog.add_property(NewPropertyEntry {
            id: 5,
            owner: PropertyOwner::EdgeLabel(7),
            name: "strength".to_string(),
            logical_type: "int64".to_string(),
            physical_encoding: "plain_i64".to_string(),
            encoding_version: 1,
            default_or_null_rule: "null".to_string(),
        });
        catalog
            .change_property_encoding(5, "float64", "plain_f64", 2, "null")
            .expect("encoding change should be recorded in catalog history");

        let registry = PropertyEncodingRegistry::from_schema_catalog(&catalog).unwrap();
        assert_eq!(
            registry.decode(5, 1, &7_i64.to_le_bytes()).unwrap(),
            PropertyValue::I64(7)
        );
        assert_eq!(
            registry.decode(5, 2, &7.25_f64.to_le_bytes()).unwrap(),
            PropertyValue::F64(7.25)
        );
    }
}
