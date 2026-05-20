import type { Task } from 'graphile-worker';

interface Payload {
  webhookUrl: string;
  urlId: string;
  clickedAt: string;
}

const fire_webhook: Task = async (payload, helpers) => {
  const { webhookUrl, urlId, clickedAt } = payload as Payload;

  const res = await fetch(webhookUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urlId, clickedAt }),
  });

  if (!res.ok) {
    throw new Error(`webhook ${webhookUrl} returned ${res.status}`);
  }

  helpers.logger.info(`webhook fired url_id=${urlId} status=${res.status}`);
};

export default fire_webhook;
