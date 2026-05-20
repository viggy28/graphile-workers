import type { Task } from 'graphile-worker';

interface Payload {
  urlId: string;
  url: string;
}

const validate_url: Task = async (payload, helpers) => {
  const { urlId, url } = payload as Payload;

  try {
    const res = await fetch(url, { method: 'HEAD', redirect: 'follow' });
    helpers.logger.info(
      `validated url_id=${urlId} status=${res.status} url=${url}`,
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    helpers.logger.warn(`validation failed url_id=${urlId} url=${url}: ${msg}`);
  }
};

export default validate_url;
