use std::{collections::HashMap, sync::Arc};
use tokio::sync::RwLock;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Default)]
pub struct SessionRegistry {
    sessions: Arc<RwLock<HashMap<String, SessionEntry>>>,
}

struct SessionEntry {
    session: AgentSession,
    cancellation: CancellationToken,
}

#[derive(Debug, Clone)]
pub struct AgentSession {
    pub connection_id: String,
    pub agent_id: String,
    pub account_id: String,
}

impl SessionRegistry {
    pub async fn register(&self, session: AgentSession) -> Result<CancellationToken, ()> {
        let mut sessions = self.sessions.write().await;
        if sessions.contains_key(&session.agent_id) {
            return Err(());
        }
        let cancellation = CancellationToken::new();
        sessions.insert(
            session.agent_id.clone(),
            SessionEntry {
                session,
                cancellation: cancellation.clone(),
            },
        );
        Ok(cancellation)
    }

    #[cfg(test)]
    pub async fn get(&self, agent_id: &str) -> Option<AgentSession> {
        self.sessions
            .read()
            .await
            .get(agent_id)
            .map(|entry| entry.session.clone())
    }

    #[cfg(test)]
    pub async fn disconnect(&self, agent_id: &str) {
        if let Some(entry) = self.sessions.write().await.remove(agent_id) {
            entry.cancellation.cancel();
        }
    }

    pub async fn remove(&self, agent_id: &str, connection_id: &str) {
        let mut sessions = self.sessions.write().await;
        if sessions
            .get(agent_id)
            .is_some_and(|entry| entry.session.connection_id == connection_id)
        {
            sessions.remove(agent_id);
        }
    }

    pub async fn count(&self) -> usize {
        self.sessions.read().await.len()
    }

    pub async fn count_for_account(&self, account_id: &str) -> usize {
        self.sessions
            .read()
            .await
            .values()
            .filter(|entry| entry.session.account_id == account_id)
            .count()
    }

    pub async fn disconnect_account(&self, account_id: &str) {
        self.sessions.write().await.retain(|_, entry| {
            if entry.session.account_id == account_id {
                entry.cancellation.cancel();
                false
            } else {
                true
            }
        });
    }
}
