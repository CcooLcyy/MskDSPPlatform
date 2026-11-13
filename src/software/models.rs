use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct PackageMetadata {
    pub product: String,
    pub version: String,
    pub platform: String,
    pub file_name: String,
    pub size: u64,
    pub sha256: String,
    #[serde(default)]
    pub signature: String,
    #[serde(default)]
    pub notes: String,
    #[serde(default)]
    pub package_version: String,
    #[serde(default)]
    pub image_id: String,
    #[serde(default)]
    pub source_repository: String,
    #[serde(default)]
    pub source_ref: String,
    #[serde(default)]
    pub source_sha: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub build_time: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub channel: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Upload {
    pub id: String,
    pub metadata: PackageMetadata,
    pub received_bytes: u64,
    pub state: String,
    pub package_id: Option<String>,
    pub created_at: u64,
    pub updated_at: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Package {
    pub id: String,
    pub metadata: PackageMetadata,
    pub created_at: String,
    pub published_at: Option<String>,
    pub channels: Vec<String>,
    pub ever_published: bool,
}

#[derive(Debug, Clone)]
pub struct DownloadFile {
    pub size: u64,
    pub sha256: String,
    pub file_name: String,
}

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("{0}")]
    Invalid(String),
    #[error("{0}")]
    NotFound(String),
    #[error("{0}")]
    Conflict(String),
    #[error("{0}")]
    Limit(String),
    #[error("{0}")]
    Storage(String),
}
