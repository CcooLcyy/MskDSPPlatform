use thiserror::Error;

#[derive(Debug, Error)]
pub enum PlatformError {
    #[error("平台协议版本不兼容: {0}")]
    UnsupportedProtocol(String),
}
