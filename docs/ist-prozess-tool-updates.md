# Ist-Prozess: Update von Drittsoftware (CLI-Tools, Helm-Charts, Pip-Pakete, Images)

**Stand:** 2026-07-23

Dieses Dokument beschreibt den aktuellen Ablauf des Update-Prozesses, wie er heute im Team durchgeführt wird.

---

## Prozessübersicht

```mermaid
flowchart TD
    A[1. Auftakt-Meeting<br>mit allen erforderlichen<br>Team-Mitgliedern] --> B[2. CVE-Identifikation<br>durch die Team-Mitglieder<br>manuell, über mehrere Tage]
    B --> C[3. Folge-Meeting<br>mit den Team-Mitgliedern:<br>Entscheidung Update ja/nein]
    C --> D{Severity der CVEs<br>HIGH?}
    D -- ja --> E[4. Manueller Download<br>jedes Artefakts:<br>CLI-Tools, Helm-Charts, etc.]
    E --> E2[Einbringen in die<br>Artifactory und auf die<br>RHEL-Maschinen]
    D -- nein --> H[Kein Update<br>in diesem Zyklus]
    E2 --> F[5. Excel-/CSV-Report<br>manuell aktualisieren]
    F --> G[6. QA durch ein weiteres<br>Team-Mitglied oder<br>eine Gruppe]
    G --> I[7. PRs für die betroffenen<br>Projekte erstellen:<br>Helm-Charts, Docker-Images, etc.]
    I --> J([Prozess abgeschlossen])
    H --> J
```

---

## Prozessschritte im Detail

### Schritt 1 — Auftakt-Meeting

Der Prozess beginnt mit einem Meeting, zu dem alle erforderlichen Team-Mitglieder eingeladen werden. In diesem Termin wird der anstehende Update-Zyklus besprochen und die Aufgabenverteilung für die CVE-Recherche festgelegt.

### Schritt 2 — CVE-Identifikation

Die Team-Mitglieder identifizieren die relevanten CVEs für die eingesetzten Tools und Komponenten. Dies geschieht manuell über verschiedene Quellen und erstreckt sich über mehrere Tage.

### Schritt 3 — Entscheidungs-Meeting

In einem weiteren Meeting werden die Rechercheergebnisse zusammengetragen. Das Team entscheidet gemeinsam, ob und welche Tools aktualisiert werden sollen.

### Schritt 4 — Beschaffung und Verteilung (bei Severity HIGH)

Ist die Severity der identifizierten CVEs HIGH, wird jedes betroffene Artefakt einzeln manuell heruntergeladen — CLI-Tools, Helm-Charts und weitere Komponenten. Anschließend werden die Artefakte in die JFrog Artifactory eingebracht und auf die RHEL-Maschinen verteilt.

### Schritt 5 — Report-Aktualisierung

Der Excel-/CSV-Report mit den Tool-Versionen wird manuell auf den neuen Stand gebracht.

### Schritt 6 — Qualitätssicherung

Ein weiteres Team-Mitglied oder eine Gruppe von Mitgliedern prüft die durchgeführten Änderungen (QA).

### Schritt 7 — Pull Requests

Für die betroffenen Projekte werden Pull Requests mit den aktualisierten Helm-Charts, Docker-Images und weiteren Anpassungen erstellt.

---

## Beteiligte Systeme und Rollen

| Element | Rolle im Prozess |
|---|---|
| Team-Mitglieder | Recherche, Entscheidung, Download, Verteilung, Report, QA, PRs |
| JFrog Artifactory | Ablage der beschafften Artefakte |
| RHEL-Maschinen | Zielsysteme für die CLI-Tools |
| Excel-/CSV-Report | Dokumentation der Versionsstände |
| Git-Repositories | Zielprojekte der PRs (Helm-Charts, Docker-Images, etc.) |
