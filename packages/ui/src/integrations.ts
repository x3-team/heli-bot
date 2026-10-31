export interface IntegrationLogo {
  id: string;
  name: string;
  src: string;
}

export const INTEGRATION_LOGOS: IntegrationLogo[] = [
  {
    id: "1c",
    name: "1С",
    src: new URL("../../../design/logos/1c.svg", import.meta.url).href,
  },
  {
    id: "bitrix24",
    name: "Битрикс24",
    src: new URL("../../../design/logos/bitrix24.svg", import.meta.url).href,
  },
  {
    id: "telegram",
    name: "Telegram",
    src: new URL("../../../design/logos/telegram.svg", import.meta.url).href,
  },
  {
    id: "yandex-360",
    name: "Яндекс 360",
    src: new URL("../../../design/logos/yandex-360.png", import.meta.url).href,
  },
  {
    id: "amocrm",
    name: "amoCRM",
    src: new URL("../../../design/logos/amocrm.png", import.meta.url).href,
  },
];
