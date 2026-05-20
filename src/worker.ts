import { run } from 'graphile-worker';
import log_click from './tasks/log_click.ts';
import validate_url from './tasks/validate_url.ts';
import fire_webhook from './tasks/fire_webhook.ts';

const connectionString =
  process.env.DATABASE_URL ??
  'postgres://urlshort:urlshort@localhost:5432/urlshort';

const main = async () => {
  const runner = await run({
    connectionString,
    concurrency: 5,
    pollInterval: 1000,
    taskList: {
      log_click,
      validate_url,
      fire_webhook,
    },
  });

  await runner.promise;
};

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
