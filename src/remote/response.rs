//! 有界数据队列之外单独保存最终状态，避免背压时丢失撤权/超时错误。
use std::{
    future::Future,
    pin::Pin,
    task::{Context, Poll},
};
use tokio::sync::{mpsc, oneshot};
use tokio_stream::Stream;
use tonic::Status;
pub struct ResponseStream<T> {
    data: mpsc::Receiver<Result<T, Status>>,
    terminal: oneshot::Receiver<Option<Status>>,
    done: bool,
}
impl<T> Unpin for ResponseStream<T> {}
impl<T> ResponseStream<T> {
    pub fn new(
        data: mpsc::Receiver<Result<T, Status>>,
        terminal: oneshot::Receiver<Option<Status>>,
    ) -> Self {
        Self {
            data,
            terminal,
            done: false,
        }
    }
}
impl<T> Stream for ResponseStream<T> {
    type Item = Result<T, Status>;
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        if this.done {
            return Poll::Ready(None);
        }
        match this.data.poll_recv(cx) {
            Poll::Ready(None) => {}
            result => return result,
        }
        match Pin::new(&mut this.terminal).poll(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(result) => {
                this.done = true;
                Poll::Ready(result.ok().flatten().map(Err))
            }
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use tokio_stream::StreamExt;
    // 数据队列已满时最终授权错误仍在队列排空后可靠送达。
    #[tokio::test]
    async fn terminal_error_survives_full_queue() {
        let (tx, rx) = mpsc::channel(1);
        let (done, end) = oneshot::channel();
        tx.send(Ok(1)).await.unwrap();
        done.send(Some(Status::permission_denied("授权失效")))
            .unwrap();
        drop(tx);
        let mut output = ResponseStream::new(rx, end);
        assert_eq!(output.next().await.unwrap().unwrap(), 1);
        assert_eq!(
            output.next().await.unwrap().unwrap_err().code(),
            tonic::Code::PermissionDenied
        );
        assert!(output.next().await.is_none());
    }
}
