use thiserror::Error;

#[derive(Debug, Error)]
pub enum PlatformError {
    #[error("Agent 标识不能为空")]
    EmptyAgentId,
    #[error("平台协议版本不兼容: {0}")]
    UnsupportedProtocol(String),
}
