# Frame Cutter

Petite application Windows locale pour extraire des images d’une vidéo à une cadence choisie. La valeur initiale est de 5 images par seconde. Les images sont écrites dans un nouveau dossier à côté de la vidéo, ou dans le dossier parent choisi.

## Démarrer en 3 étapes

1. **Installer Python 3.11 ou plus récent** (environ 2 minutes). [Télécharger Python pour Windows](https://www.python.org/downloads/windows/), lancez l’installation et gardez les options proposées.
2. **Ajouter le composant vidéo FFmpeg** (environ 2 minutes). [Télécharger FFmpeg pour Windows](https://ffmpeg.org/download.html#build-windows), décompressez l’archive, puis copiez tout le contenu de son dossier `bin` dans le dossier `tools\ffmpeg\bin` de Frame Cutter (créez ce dossier s’il n’existe pas).
3. **Double-cliquez sur `Lancer Frame Cutter.bat`.** La fenêtre s’ouvre. S’il manque quelque chose, un message en français indique la cause et l’action à faire, puis attend que vous l’ayez lu.

Si le composant vidéo est absent, Frame Cutter l’annonce en haut de la fenêtre dès l’ouverture.

## Premier essai

Ouvrez une courte vidéo MP4 avec **Ouvrir un fichier vidéo…**. Gardez la vidéo entière, choisissez **Une image toutes les N secondes**, saisissez **5** dans **Secondes entre les images**, sélectionnez **PNG**, puis cliquez sur **Extraire les images**. À la fin, la barre d’état indique le nombre d’images enregistrées et le nom du dossier créé, par exemple « Terminé : 4 images enregistrées dans essai_frames. ». Cliquez sur **Ouvrir le dossier** pour retrouver les images.

## Utilisation

1. **Choisir la vidéo.** Cliquez sur **Ouvrir un fichier vidéo…** ou sur **Importer un lien…**.
2. **Choisir les passages.** Déplacez la poignée de la barre **Position vidéo** pour voir une autre image. Utilisez **Marquer le début** et **Marquer la fin** ou saisissez les deux valeurs. Pour plusieurs passages, cliquez sur **Ajouter ce passage** après chaque paire de bornes.
3. **Régler l’export.** Choisissez la cadence : 1 à 60 images/s, ou une image toutes les N secondes (de 1 à 3 600), le format JPEG ou PNG et le dossier de sortie. Vérifiez l’estimation, puis cliquez sur **Extraire les images**.
4. **Images exportées.** Parcourez les résultats dans la galerie.

Les bornes acceptent les secondes (`12,5`) et les minutages (`01:12,5` ou `1:01:12,5`). L’instant de fin est exclu. Quand la liste est vide, l’extraction utilise les bornes affichées. **Vidéo entière** rétablit les bornes de la source.

Le dossier de sortie proposé suit la vidéo locale ouverte. Si vous choisissez un dossier, Frame Cutter le conserve pour les vidéos suivantes.

Quand la liste **Passages à extraire** est remplie, seules ses sélections sont exportées, dans l’ordre de la vidéo. Les passages peuvent se toucher mais un chevauchement est signalé. Pour retirer une entrée, sélectionnez-la dans la liste et cliquez sur **Retirer le passage sélectionné**. Les images sont numérotées à la suite dans un seul dossier et apparaissent dans la même galerie. Le changement de vidéo vide la liste.

Le panneau **Images exportées** s’affiche dans la fenêtre principale dès la fin de l’extraction. En fenêtre large, les réglages et l’aperçu vidéo restent côte à côte en haut, et la galerie apparaît sur toute la largeur du contenu en dessous. Faites défiler ou cliquez sur **Voir les images** pour y accéder directement.

En format étroit, le contenu défile sur une colonne. La galerie passe en premier après l’export et **Masquer** fait revenir l’aperçu. Sur les fenêtres les plus étroites, un sélecteur permet de choisir une image. Les commandes et la progression restent visibles en bas de la fenêtre.

Cliquez sur un nom de fichier ou utilisez **Précédente** et **Suivante**. **Supprimer…** affiche la confirmation dans le panneau. **Supprimer définitivement** efface le fichier sélectionné du dossier. **Ouvrir le dossier** affiche les fichiers dans l’Explorateur Windows.

Utilisez **Annuler** pour arrêter un import, une analyse ou une extraction. La fermeture attend l’arrêt du traitement. Les deux boutons de résultat sont aussi disponibles pour un export partiel si un dossier a été créé.

Le moteur sélectionne des images existantes à intervalle régulier. Il ne crée pas d’images interpolées. Les résultats sont nommés `frame_000001.jpg` ou `.png` dans un dossier `nom_video_frames`. Si ce dossier existe déjà, un suffixe numérique est ajouté pour éviter de remplacer son contenu.

## Importer un lien

Importez uniquement des vidéos que vous avez le droit de télécharger et de traiter. Pour un lien de plateforme, vérifiez aussi que le mode d’accès et de téléchargement est autorisé par le service. Une vidéo visible publiquement peut être soumise à ces restrictions. Si vous disposez déjà d’une copie obtenue par un moyen autorisé, ouvrez le fichier local. [Conditions YouTube](https://www.youtube.com/t/terms) · [Conditions Instagram](https://help.instagram.com/581066165581870/)

YouTube et Instagram demandent des composants supplémentaires, décrits dans la section [Import depuis un lien (avancé)](#import-depuis-un-lien-avancé). Sans eux, la boîte d’import ne propose que les liens directs et explique pourquoi les plateformes sont indisponibles.

- **YouTube** accepte les liens de vidéos publiques et de Shorts lisibles sans connexion. Renseignez les deux bornes pour demander un seul extrait ou laissez-les vides pour importer la vidéo. Après l’import d’un extrait, le curseur démarre à **00:00** au début de celui-ci. Des données proches des bornes peuvent être nécessaires au décodage.
- **Instagram** accepte les liens `/reel/…` publics accessibles sans connexion. Frame Cutter récupère le Reel entier, puis vous choisissez les passages dans l’aperçu.
- **Fichier direct en HTTPS** accepte par exemple un MP4 ou un WebM hébergé sur un site. Le fichier entier est récupéré, puis vous choisissez vos passages dans Frame Cutter.

La vidéo importée est stockée dans un dossier temporaire. Les imports sont limités à 1 Go et s’arrêtent si l’espace libre descend sous 512 Mo. Le téléchargement depuis un lien direct utilise une connexion HTTPS publique, y compris après une redirection. Si vous utilisez un serveur privé ou un réseau qui impose un proxy, ouvrez la vidéo comme fichier local. En cas de coupure réseau, Frame Cutter affiche « Connexion impossible ou interrompue. Vérifiez votre accès Internet puis réessayez. ».

Frame Cutter nettoie ses dossiers temporaires quand on change de source ou qu’on ferme l’application. Au redémarrage, il efface aussi les dossiers d’import qu’il a créés depuis cette version et qui ne sont plus utilisés. Les images restent dans le dossier de sortie sélectionné.

### Enregistrer sans ouvrir

Dans la boîte d’import, **Importer** ouvre la vidéo dans Frame Cutter. **Enregistrer sans ouvrir…** garde seulement une copie : il demande un dossier, mais n’ouvre pas la vidéo dans la fenêtre. Frame Cutter réutilise le téléchargement et la validation vidéo, prépare la copie sous un nom temporaire puis la renomme une fois vérifiée. Un fichier déjà présent n’est jamais remplacé : un suffixe numérique est ajouté. La barre d’état indique le nom du fichier et celui de son dossier, et le fichier reste disponible après la fermeture de l’application. Pour le traiter, ouvrez-le ensuite avec **Ouvrir un fichier vidéo…**. Pour une plateforme, l’enregistrement demande les pistes audio disponibles et les remuxe en MKV.

## Import depuis un lien (avancé)

Cette section n’est utile que pour importer des liens YouTube ou Instagram. Les fichiers locaux et les liens directs fonctionnent sans elle.

1. Pour YouTube, installez Node.js 22 ou plus récent avec les options proposées. [Télécharger Node.js pour Windows](https://nodejs.org/en/download). Le programme d’installation l’ajoute au `PATH`.
2. Dans PowerShell, ouvrez le dossier qui contient `app.py` et installez les composants d’import dans le projet :

```powershell
py -3 -m pip install --target tools/link-import -r requirements-import.txt
```

La version de `yt-dlp` est épinglée et vérifiée par empreinte. Frame Cutter refuse toute autre version et l’indique dans la boîte d’import. Relancez ensuite `Lancer Frame Cutter.bat`. Si une plateforme bloque un lien, Frame Cutter affiche l’erreur et permet de réessayer ou d’ouvrir un fichier local.

### Lancement manuel et vidéo d’essai

Le lanceur équivaut à `py -3 app.py` dans le dossier de Frame Cutter. Si FFmpeg est déjà dans le `PATH`, il est utilisé quand `tools\ffmpeg\bin` est vide. Pour créer une vidéo synthétique d’essai, lancez dans PowerShell (l’option `-n` préserve un fichier déjà présent) :

```powershell
& .\tools\ffmpeg\bin\ffmpeg.exe -n -f lavfi -i "testsrc2=size=160x90:rate=25" -t 20 -c:v mpeg4 essai.mp4
```

## Limites connues

- Les formats lisibles dépendent des codecs inclus dans la compilation FFmpeg choisie.
- Le nombre d’images est estimé à partir de la durée et de la cadence sélectionnée ; les cadences variables et les horodatages peuvent produire un résultat légèrement différent.
- Une cadence élevée, une longue vidéo ou le format PNG peut demander beaucoup de temps et d’espace disque. L’estimation affiche un avertissement à partir de 10 000 images.
- Une annulation ou une erreur conserve les images déjà écrites. Le dernier fichier peut être incomplet si l’arrêt intervient pendant son écriture.

## Vérifier les corrections

Depuis la racine du projet, sur Windows avec Python, Tkinter, FFmpeg et ffprobe disponibles :

```powershell
python -B -m unittest discover -s tests -v
```

Les tests génèrent de courtes vidéos dans `.test-work`, puis nettoient leurs propres fichiers. Ils vérifient notamment l’aperçu, la sélection de portion, l’affichage des JPEG et PNG dans la galerie, la suppression confirmée et la fermeture pendant un traitement.

Les dépendances et leurs projets d’origine sont listés dans [THIRD-PARTY.md](THIRD-PARTY.md).

Les propositions de modification sont bienvenues. Voir [Contribuer à Frame Cutter](CONTRIBUTING.md).

## Licence

Le code de Frame Cutter est sous [licence MIT](LICENSE). Les outils installés séparément conservent leurs [licences respectives](THIRD-PARTY.md).
