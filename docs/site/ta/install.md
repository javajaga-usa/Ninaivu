# நிறுவுதல்

எப்போதும் இயங்கிக்கொண்டிருக்கும் ஒரு கணினியில் நினைவு இயங்குகிறது: படிக்கட்டுக்குக்
கீழே ஒரு desktop, ஒரு mini PC, ஒரு பழைய laptop, ஒரு Mac mini, Docker இயக்கும் ஒரு
NAS. ஒரு checkout-இலிருந்து இயக்கினால் Python 3.12 அல்லது அதற்குப் புதியது தேவை;
நிறுவிகள் (installers) தங்களுக்கு வேண்டியதைத் தாங்களே கொண்டு வருகின்றன.

## Windows

[சமீபத்திய வெளியீட்டிலிருந்து](https://github.com/javajaga-usa/Ninaivu/releases/latest)
`Ninaivu-<version>-windows-x64.exe` கோப்பைப் பதிவிறக்கி இயக்குங்கள். இது உங்கள்
கணக்குக்கு மட்டும் நிறுவப்படும் (நிர்வாகி அனுமதி கேட்காது); தனியான Python-ஐயும்
நினைவுக்குத் தேவையான அனைத்தையும் `%LOCALAPPDATA%\Programs\Ninaivu` கீழ் வைத்து,
Start menu-வில் **Ninaivu**-வைச் சேர்க்கும். *Start Ninaivu at sign-in* என்பதைத்
தேர்ந்தெடுத்தபடியே விட்டால், அது எப்போதும் தயாராக இருக்கும்.

அல்லது, manifest இணைக்கப்பட்ட பின்:

```bash
winget install Ninaivu.Ninaivu
```

**Ninaivu**-வைத் திறந்தால் system tray-இல் ஒரு சின்னம் (icon) தோன்றும். அதன் menu,
server-ஐத் தொடங்கவும் நிறுத்தவும், குடும்பச் செயலியையும் நிர்வாக பலகையையும்
திறக்கவும் உதவும். [Tray பற்றி மேலும் (ஆங்கிலம்).](../../desktop-control.md)

## macOS

உங்கள் Mac-க்கான `.dmg` கோப்பைப் பதிவிறக்குங்கள் — Apple silicon-க்கு `arm64`,
Intel-க்கு `x86_64` —
[சமீபத்திய வெளியீட்டிலிருந்து](https://github.com/javajaga-usa/Ninaivu/releases/latest).
அதைத் திறந்து **Ninaivu**-வை Applications-க்கு இழுத்துவிடுங்கள். அல்லது:

```bash
brew install --cask ninaivu
```

நினைவு Dock-இல் அல்ல, menu bar-இல் இருக்கும். அதன் menu server-ஐத் தொடங்கவும்
நிறுத்தவும், குடும்பச் செயலியையும் நிர்வாக பலகையையும் திறக்கவும், கணினியில்
உள்நுழையும்போதே தானாகத் தொடங்கவும் உதவும்.

## Docker

ஒரு NAS அல்லது Linux கணினிக்கு:

```bash
git clone https://github.com/javajaga-usa/Ninaivu.git
cd Ninaivu
cp installers/docker/.env.example installers/docker/.env   # set MEDIA_DIR to your photo folder
docker compose -f installers/docker/docker-compose.yml up -d
```

(`.env` கோப்பில் `MEDIA_DIR` என்பதை உங்கள் புகைப்படக் கோப்புறைக்கு அமையுங்கள்.)

குடும்பச் செயலி port 5000-இலும், நிர்வாக பலகை port 3000-இலும் பதில் அளிக்கும்.
systemd, reverse proxy, HTTPS, பெரிய நூலகங்கள் பற்றி
[operations வழிகாட்டி (ஆங்கிலம்)](../../operations/production.md) விளக்குகிறது.

## ஒரு checkout-இலிருந்து

```bash
git clone https://github.com/javajaga-usa/Ninaivu.git
cd Ninaivu
start.cmd ~/Pictures                 # Windows: or double-click start.cmd
sh launcher/start.sh ~/Pictures      # macOS and Linux
```

Windows-இல் `start.cmd` கோப்பை இருமுறை சொடுக்கினாலும் போதும். Launcher ஒரு
virtual environment உருவாக்கி, இல்லாதவற்றை நிறுவி, காலியான port-களைக் கண்டுபிடித்து,
உங்கள் உலாவியைத் திறக்கும்.

## முதலில் நீங்கள் பார்ப்பது

முதல் திரை நிர்வாகியை (administrator) உருவாக்குகிறது. அதன் பிறகு நிர்வாக பலகை
உங்களை [முதல் நாள்](first-day.md) வழியாக அழைத்துச் செல்லும்.

!!! tip "இரண்டு முகவரிகள்"
    நினைவுக்கு இரண்டு port-களில் இரண்டு முகங்கள் உண்டு: எல்லோரும் நூலகத்தைப்
    பார்க்கும் **குடும்பச் செயலி**, நிர்வாகி அதை நடத்தும் **நிர்வாக பலகை** (console).
    இயல்பாக, நிர்வாக பலகை அந்தக் கணினியில் மட்டுமே திறக்கும்; குடும்பச் செயலி
    வீட்டு network முழுவதும் கிடைக்கும்.
