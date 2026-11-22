# Frame Cutter

Petite application Windows locale pour extraire des images d’une vidéo à une cadence choisie. La valeur initiale est de 5 images par seconde. Les images sont écrites dans un nouveau dossier à côté de la vidéo, ou dans le dossier parent choisi.

## Prérequis sous Windows

- Windows 10 ou 11 et Python 3.14 avec Tkinter. [Télécharger Python pour Windows](https://www.python.org/downloads/windows/).
- Une distribution Windows de FFmpeg qui fournit `ffmpeg.exe` et `ffprobe.exe`. Placez le dossier `bin` dans le `PATH`, ou copiez ces exécutables et les DLL qui les accompagnent dans `tools/ffmpeg/bin` à côté de `app.py`. [Télécharger FFmpeg pour Windows](https://ffmpeg.org/download.html#build-windows).

Dans PowerShell, ouvrez le dossier qui contient `app.py` et lancez `python -m tkinter`. Une fenêtre de démonstration doit s’ouvrir. Fermez-la pour continuer.

## Lancement

Dans le même dossier PowerShell, lancez l’application.

```powershell
python app.py
```

## Premier essai

Pour essayer avec une vidéo synthétique, lancez cette commande avant d’ouvrir l’application. Si FFmpeg est dans `tools/ffmpeg/bin`, commencez la commande par `& .\tools\ffmpeg\bin\ffmpeg.exe` à la place de `ffmpeg`. L’option `-n` préserve un fichier d’essai déjà présent.

```powershell
ffmpeg -n -f lavfi -i "testsrc2=size=160x90:rate=25" -t 20 -c:v mpeg4 essai.mp4
```

Dans Frame Cutter, ouvrez `essai.mp4`. Gardez la vidéo entière, choisissez **Une image toutes les N secondes**, saisissez **5** dans **Secondes entre les images**, sélectionnez **PNG**, puis cliquez sur **Extraire les images**. Le dossier `essai_frames` contient quatre images, de `frame_000001.png` à `frame_000004.png`. Si ce dossier existe déjà, regardez le nouveau dossier numéroté affiché par l’application.

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

- **YouTube** accepte les liens de vidéos publiques et de Shorts lisibles sans connexion. Renseignez les deux bornes pour demander un seul extrait ou laissez-les vides pour importer la vidéo. Après l’import d’un extrait, le curseur démarre à **00:00** au début de celui-ci. Des données proches des bornes peuvent être nécessaires au décodage.
- **Instagram** accepte les liens `/reel/…` publics accessibles sans connexion. Frame Cutter récupère le Reel entier, puis vous choisissez les passages dans l’aperçu.
- **Fichier direct en HTTPS** accepte par exemple un MP4 ou un WebM hébergé sur un site. Le fichier entier est récupéré, puis vous choisissez vos passages dans Frame Cutter.

La vidéo importée est stockée dans un dossier temporaire. Les imports sont limités à 1 Go et s’arrêtent si l’espace libre descend sous 512 Mo. Le téléchargement depuis un lien direct utilise une connexion HTTPS publique, y compris après une redirection. Si tu utilises un serveur privé ou un réseau qui impose un proxy, ouvre la vidéo comme fichier local.

Frame Cutter nettoie ses dossiers temporaires quand on change de source ou qu’on ferme l’application. Au redémarrage, il efface aussi les dossiers d’import qu’il a créés depuis cette version et qui ne sont plus utilisés. Les images restent dans le dossier de sortie sélectionné.

### Enregistrer une copie

Dans la boîte d’import, **Enregistrer une copie…** demande un dossier. Frame Cutter réutilise le téléchargement et la validation vidéo, prépare la copie sous un nom temporaire puis la renomme une fois vérifiée. Un fichier déjà présent n’est jamais remplacé : un suffixe numérique est ajouté. Le chemin final apparaît dans la barre d’état et le fichier reste disponible après la fermeture de l’application. Pour une plateforme, l’enregistrement demande les pistes audio disponibles et les remuxe en MKV.

### Composants pour YouTube et Instagram

L’import de liens YouTube ou Instagram demande les composants Python ci-dessous. Pour YouTube, ajoutez également Node.js 22 ou plus récent au `PATH`. [Télécharger Node.js pour Windows](https://nodejs.org/en/download).

Installez les composants d’import dans le projet depuis PowerShell :

```powershell
python -m pip install --target tools/link-import -r requirements-import.txt
```

Les fichiers directs et l’ouverture locale sont disponibles sans ces composants. Si une plateforme bloque un lien, Frame Cutter affiche l’erreur et permet de réessayer ou d’ouvrir un fichier local.

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
