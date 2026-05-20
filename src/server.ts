import Fastify from 'fastify';
import { customAlphabet } from 'nanoid';
import { makeWorkerUtils, type WorkerUtils } from 'graphile-worker';
import { pool } from './db.ts';

const shortCode = customAlphabet(
  '123456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ',
  6,
);

const app = Fastify({ logger: true });
let workerUtils: WorkerUtils;

app.post<{ Body: { url?: string; webhookUrl?: string } }>(
  '/shorten',
  async (req, reply) => {
    const { url, webhookUrl } = req.body ?? {};
    if (!url || typeof url !== 'string') {
      return reply.code(400).send({ error: 'url (string) required' });
    }

    const code = shortCode();
    const client = await pool.connect();
    try {
      await client.query('BEGIN');

      const { rows } = await client.query<{ id: string }>(
        'INSERT INTO urls (short_code, original_url, webhook_url) VALUES ($1, $2, $3) RETURNING id',
        [code, url, webhookUrl ?? null],
      );
      const urlId = rows[0].id;

      // validate_url enqueued *inside* the urls INSERT transaction.
      // Either both rows commit or neither does.
      await client.query(
        "SELECT graphile_worker.add_job('validate_url', $1::json)",
        [JSON.stringify({ urlId, url })],
      );

      await client.query('COMMIT');
    } catch (err) {
      await client.query('ROLLBACK');
      throw err;
    } finally {
      client.release();
    }

    const host = req.headers.host ?? `localhost:${process.env.PORT ?? 8080}`;
    return { shortUrl: `http://${host}/${code}`, code };
  },
);

app.get<{ Params: { code: string } }>('/:code', async (req, reply) => {
  const { code } = req.params;

  const { rows } = await pool.query<{
    id: string;
    original_url: string;
    webhook_url: string | null;
  }>(
    'SELECT id, original_url, webhook_url FROM urls WHERE short_code = $1',
    [code],
  );

  if (rows.length === 0) {
    return reply.code(404).send({ error: 'not found' });
  }

  const { id, original_url, webhook_url } = rows[0];

  await workerUtils.addJob('log_click', {
    urlId: id,
    ip: req.ip,
    userAgent: req.headers['user-agent'] ?? null,
  });

  if (webhook_url) {
    await workerUtils.addJob(
      'fire_webhook',
      {
        webhookUrl: webhook_url,
        urlId: id,
        clickedAt: new Date().toISOString(),
      },
      { maxAttempts: 5 },
    );
  }

  return reply.redirect(original_url, 302);
});

app.post('/mock-webhook', async (req, reply) => {
  if (Math.random() < 0.6) {
    req.log.info('mock-webhook: simulated failure (500)');
    return reply.code(500).send({ error: 'simulated failure' });
  }
  req.log.info('mock-webhook: accepted (200)');
  return { ok: true };
});

const main = async () => {
  workerUtils = await makeWorkerUtils({
    connectionString:
      process.env.DATABASE_URL ??
      'postgres://urlshort:urlshort@localhost:5432/urlshort',
  });

  const port = Number(process.env.PORT ?? 8080);
  await app.listen({ port, host: '0.0.0.0' });
};

main().catch((err) => {
  app.log.error(err);
  process.exit(1);
});
