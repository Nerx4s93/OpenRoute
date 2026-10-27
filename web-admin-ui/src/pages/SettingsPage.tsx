import { Section } from "../components/Section";
import { LANGUAGES, useI18n, type Language } from "../i18n";

function languageName(language: Language): "settings.english" | "settings.russian" {
  return language === "en" ? "settings.english" : "settings.russian";
}

function languageHint(language: Language): "settings.englishHint" | "settings.russianHint" {
  return language === "en" ? "settings.englishHint" : "settings.russianHint";
}

export function SettingsPage() {
  const { language, setLanguage, t } = useI18n();

  return (
    <>
      <div className="hero page-enter">
        <div className="hero-content">
          <div className="eyebrow">{t("settings.eyebrow")}</div>
          <h1>{t("settings.title")}</h1>
        </div>
      </div>

      <Section
        id="language-title"
        icon="⚙"
        title={t("settings.languageTitle")}
        note={t("settings.languageNote")}
      >
        <div className="settings-content">
          <div className="settings-copy">
            <h3>{t("settings.languageLabel")}</h3>
          </div>
          <div
            className="language-grid"
            role="group"
            aria-label={t("settings.languageLabel")}
          >
            {LANGUAGES.map((option) => {
              const active = option === language;
              return (
                <button
                  key={option}
                  type="button"
                  className={`language-option${active ? " active" : ""}`}
                  aria-pressed={active}
                  onClick={() => setLanguage(option)}
                >
                  <span className="language-option-head">
                    <strong>{t(languageName(option))}</strong>
                    {active ? (
                      <span className="language-check">{t("settings.selected")}</span>
                    ) : null}
                  </span>
                  <span className="language-option-hint">{t(languageHint(option))}</span>
                </button>
              );
            })}
          </div>
        </div>
      </Section>
    </>
  );
}
