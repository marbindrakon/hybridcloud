import './consoleK8sBootstrap';

// Do not call initI18n() here — shared react-i18next would stomp console i18n.
// Page components init an isolated instance via useTranslation().

export { default as ShcConsolePage } from './ShcConsolePage';
export { icon, getLandingPageURL, getImportRedirectURL } from './perspective';
