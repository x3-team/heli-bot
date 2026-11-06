/** Short how-to under «Подключения»: what to put in Сайт / Имя / Секрет for mail and business services. */

export interface ConnectRecipe {
  id: string;
  title: string;
  site: string;
  login: string;
  secret: string;
  note: string;
}

export const CONNECT_RECIPES: ConnectRecipe[] = [
  {
    id: "mail-public",
    title: "Почта Яндекс, Mail.ru, Gmail",
    site: "yandex.ru, mail.ru, gmail.com или imap-сервер (imap.yandex.ru)",
    login: "полный адрес ящика, например shop@yandex.ru",
    secret: "пароль приложения из настроек почты, не основной пароль ящика",
    note: "В настройках почты включите доступ по IMAP.",
  },
  {
    id: "mail-domain",
    title: "Почта на своём домене",
    site: "IMAP-сервер из настроек хостинга, например imap.company.ru (Beget: imap.beget.com)",
    login: "полный адрес, например info@company.ru",
    secret: "пароль ящика или пароль приложения",
    note: "Просто домен company.ru не подойдёт: нужен адрес сервера, который начинается с imap.",
  },
  {
    id: "bitrix24",
    title: "Битрикс24",
    site: "адрес портала, например company.bitrix24.ru",
    login: "webhook",
    secret: "адрес входящего вебхука (Разработчикам → Другое → Входящий вебхук)",
    note: "Так бот читает CRM без входа на экране и без капчи.",
  },
  {
    id: "moysklad",
    title: "МойСклад",
    site: "online.moysklad.ru",
    login: "логин МоегоСклада (или token)",
    secret: "пароль (или токен доступа к API)",
    note: "Бот читает остатки, товары и документы через API.",
  },
  {
    id: "direct",
    title: "Яндекс Директ и Метрика",
    site: "direct.yandex.ru (для Метрики можно metrika.yandex.ru)",
    login: "token",
    secret: "OAuth-токен Яндекса с доступом к API Директа и Метрики",
    note: "Боевой кабинет бот только читает.",
  },
];

export function ConnectHelp() {
  return (
    <details className="connect-help" data-testid="connect-help">
      <summary>Как подключить почту и сервисы</summary>
      <p className="muted">
        Пароли и токены не пишите в чат: только сюда. Почту бот читает, не помечая письма прочитанными. Письма
        отправляет только после вашего подтверждения и только если отправка включена на сервере. Капчу бот не
        решает: попросит пройти её вас.
      </p>
      <ul className="connect-recipes">
        {CONNECT_RECIPES.map((recipe) => (
          <li key={recipe.id}>
            <strong>{recipe.title}</strong>
            <span>Сайт: {recipe.site}</span>
            <span>Имя: {recipe.login}</span>
            <span>Секрет: {recipe.secret}</span>
            <span className="muted">{recipe.note}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
