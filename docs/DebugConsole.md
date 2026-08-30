# Hex DevConsole Commands

Open the console by pressing **backtick/tilde** (`` ` ``) after login.

## Account
| Command | Description |
|---|---|
| `account.autologin` | Login Automatically |

## Arena
| Command | Description |
|---|---|
| `arena.assign_deck` | Assigns a deck and creates an arena instance |
| `arena.buyout` | Forces a buyout to get past the first arena tier |
| `arena.cheatwin` | True for victory, False for lose |
| `arena.cleanup` | Cleans up all the data related to an ARENA |
| `arena.forcechallenge` | Forces an MC Challenge into the current fight based off challenge number |
| `arena.join` | Attempts to join an existing arena - if one exists |
| `arena.launch` | Launch the Arena UI section |
| `arena.resetlocal` | Resets all arena local player preferences |
| `arena.resetlosses` | Resets the Losses to Zero - allows for arena testing to continue w/o cash out |
| `arena.setChallenger` | Attempts to change next fight to new ID |
| `arena.showallloot` | Shows all arena loot by difficulty |

## AI / Neural Net
| Command | Description |
|---|---|
| `ai.evaluate` | Evaluates a card on the specified state |
| `ai.neuralnet.autotrain` | Train the neural network given the specified file |
| `ai.neuralnet.test` | Test the neural network with a deck |
| `ai.neuralnet.train` | Train the neural network with a deck |

## Asset Bundle Manager
| Command | Description |
|---|---|
| `assetbundlemanager.listbundles` | List currently loaded bundles |
| `assetbundlemanager.unload` | Manually unload an asset bundle |
| `assetbundlemanager.viewbundle` | View a bundle's current loaded contents |

## Auction
| Command | Description |
|---|---|
| `auction.bid` | Place a bid on an auction |
| `auction.card` | Puts a card from inventory up for auction |
| `auction.cleanup` | Cleans up expired auctions |
| `auction.lock` | Lock the auction server |
| `auction.searchcards` | Search card auctions |
| `auction.setexpiry` | Set expiry on selected auction |
| `auction.unlock` | UnLock the auction server |

## Campaign
| Command | Description |
|---|---|
| `camp.Adventure` | Start adventure zone |
| `camp.Dungeon` | Start Dungeon |
| `camp.Pano` | Start Panorama |
| `camp.Stronghold` | Enter Stronghold |
| `camp.addxp` | Adjust the current campaign champions' XP |
| `camp.encounter` | Start an encounter with the given name |
| `camp.setpartycap` | Change the party cap for your account |
| `camp.setpetname` | Set the current champion's pet name |
| `camp.state_get` | Get the current campaign state |
| `camp.state_set` | Forfeit all state and set state to specified saveFile |
| `camp.win_dungeon` | Auto win the campaign state if it is a dungeon |
| `campaign.deleteFlag` | Deletes a flag but does not remove any items awarded |
| `campaign.setProgress` | Change the current progress of a flag |
| `campaign.showAllFlags` | Shows all the progression flags |

## Cards
| Command | Description |
|---|---|
| `card.bancard` | Ban a card by name |
| `card.search` | Search card template database |
| `card.unbancard` | Un-ban a card by name |

## Champions
| Command | Description |
|---|---|
| `champion.create` | Show Create Champion interface |
| `champion.talents` | Show Create Champion Talents interface |

## Cheats (In-Game)
| Command | Description |
|---|---|
| `cheat.addall` | Add resources, thresholds and charges to yourself |
| `cheat.addcard` | Add a card to your hand, costs included |
| `cheat.addcardbyid` | Add a card by id to your hand, costs included |
| `cheat.addcardtotopofdeck` | Add a card to the top of your deck |
| `cheat.addcardtotopofenemydeck` | Add a card to the top of opponent's deck |
| `cheat.addcharges` | Add X charges |
| `cheat.addenemyall` | Add resources, thresholds and charges to opponent |
| `cheat.addenemycard` | Add a card to opponent's hand |
| `cheat.addenemycardbyid` | Add a card by id to opponent's hand |
| `cheat.addenemycharges` | Add X charges to opponent |
| `cheat.addenemyresources` | Add resources to opponent |
| `cheat.addenemyspellpoints` | Add X spell points to opponent |
| `cheat.addenemythreshold` | Add X threshold to opponent |
| `cheat.addnthcard` | Add the nth matching card to your hand |
| `cheat.addnthenemycard` | Add the nth matching card to opponent's hand |
| `cheat.addresources` | Add resources to yourself |
| `cheat.addspellpoints` | Add X spell points |
| `cheat.addthreshold` | Add X threshold |
| `cheat.bury` | Bury x cards |
| `cheat.cleartalents` | Clear talents |
| `cheat.configureenemyai` | Configure AI behavior |
| `cheat.destroycard` | Kill a card in the warzone |
| `cheat.discardhand` | Discard your hand |
| `cheat.draw` | Draw x cards |
| `cheat.enemybury` | Bury x enemy cards |
| `cheat.enemydiscardhand` | Discard enemy hand |
| `cheat.enemydraw` | Draw x cards for opponent |
| `cheat.enemyplay` | Add card to opponent's hand and immediately play it |
| `cheat.enemyplaybyid` | Play a card by id into opponent's area |
| `cheat.hideenemyhand` | Makes the enemy's hand invisible |
| `cheat.hideunderground` | Makes the enemy's underground invisible |
| `cheat.home` | Returns to the landing page |
| `cheat.notimers` | Set game timers to very large values |
| `cheat.nuke` | Void ALL cards then add 10 wild shards to both decks |
| `cheat.play` | Add a card to your hand and play it |
| `cheat.playbyid` | Play a card by id |
| `cheat.playcard` | Play a card (no cost) |
| `cheat.playenemycard` | Play a card into opponent's area (no cost) |
| `cheat.publishevent` | Trigger server to publish an event |
| `cheat.randomizewarzones` | Randomize the warzones |
| `cheat.removeenemyequipment` | Remove equipment from opponent's champion |
| `cheat.removeequipment` | Remove equipment from your champion |
| `cheat.removetalent` | Remove talent by name |
| `cheat.removetalentbyindex` | Remove talent by index |
| `cheat.setDefaultGems` | Set gems in a socketed card (gem type = hex value) |
| `cheat.setaideck` | Makes AI use one of your decks by name |
| `cheat.setbattleboard` | Set the current battle board |
| `cheat.setdeckdais` | Set the current deck dais |
| `cheat.setenemyequipment` | Set equipment on opponent's champion |
| `cheat.setenemyequipmentbyindex` | Set equipment on opponent's champion by index |
| `cheat.setenemylife` | Set enemy champion life |
| `cheat.setequipment` | Set equipment on your champion |
| `cheat.setequipmentbyindex` | Set equipment on your champion by index |
| `cheat.setlife` | Set champion life |
| `cheat.settalent` | Set talent by name |
| `cheat.settalentbyindex` | Set talent by index |
| `cheat.showchoosing` | Show cards in choosing zone |
| `cheat.showenemyhand` | Makes the enemy's hand partially visible |
| `cheat.showunderground` | Makes the enemy's underground zone partially visible |
| `cheat.spin` | Upgrade the chest with the given ID |
| `cheat.toggleui` | Shows a particular UI (With tag) |
| `cheat.tournament` | Goes to the tournament scene |
| `cheat.transformchampion` | Transform your champion by name |
| `cheat.transformchampionbyindex` | Transform your champion by index |
| `cheat.transformenemychampion` | Transform enemy champion by name |
| `cheat.transformenemychampionbyindex` | Transform enemy champion by index |
| `cheat.transformmercenary` | Transform your mercenary by name |
| `cheat.transformmercenarybyindex` | Transform your mercenary by index |

## Chest Cheats
| Command | Description |
|---|---|
| `chestcheats.awardchests` | Award a number of chests to the player |
| `chestcheats.awardchestsdefaultcardset` | Award chests to the player (default cardset) |
| `chestcheats.cheatopen` | Open the selected chest and get ALL THE LOOT! |
| `chestcheats.getchests` | List chests for the current player |
| `chestcheats.resetchest` | Set chest to have a free spin |
| `chestcheats.upgradechest` | Upgrade the chest with the given ID |

## Debug
| Command | Description |
|---|---|
| `debug` | Enables or disables debug output in the console |
| `debug.abilities` | Display state of ability manager |
| `debug.card` | Display state of a card |
| `debug.greenlight` | Gain greenlight |
| `debug.loadscene` | Load a scene by name |
| `debug.maxsetnum` | Gets maximum allowed set number |
| `debug.memorytracker` | Toggle the Memory Tracker overlay |
| `debug.practice` | Practice against the AI |
| `debug.session.state` | Show the UI game session state |
| `debug.showdeckload` | Show new deck loader with folder hierarchy |
| `debug.showhelper` | Show Calliope avatar on screen |
| `debug.showplayable` | Recalculate highlighted cards |
| `debug.showreserves` | Display the reserves toggle in deck builder |
| `debug.textbubble` | Display text bubble over speaker's head |
| `debug.textbubblebanter` | Display a series of dismissable text bubbles |
| `debug.textbubbleseries` | Display a series of dismissable text bubbles |
| `debug.ui` | Toggle Debug UI (FPS, errors, build label) |
| `debug.unload` | Unload Unused Assets |

## Deck Editor
| Command | Description |
|---|---|
| `deck.setchampion` | Set the active champion in the editor (save required) |
| `deck.setchampionbyindex` | Set champion by index (save required) |
| `deck.setequipment` | Set the active equipment in the editor (save required) |

## Dump / Diagnostics
| Command | Description |
|---|---|
| `dump.error` | Dump errors |
| `dump.objects` | Dump objects |
| `dump.textures` | Dump textures |

## Hints & Tutorials
| Command | Description |
|---|---|
| `hint.arrow` | Test hint arrow |
| `hint.deckmanager_deckadjustment` | Test DeckManager Hint |
| `hint.deckmanager_firsttime` | Test DeckManager First Time Hint |
| `hint.globescreen` | Test Globe Screen Hint |
| `hint.hint` | Test Hint Window |
| `hints.reset` | Reset hints of currently logged in player |
| `tutorial.shownext` | Displays the next pending tutorial action |
| `tutorial.skipaction` | Skips the currently pending tutorial action |
| `tb.background0` | Use tutorial background 0 |
| `tb.background1` | Use tutorial background 1 |
| `tb.background2` | Use tutorial background 2 |
| `tb.close` | Close tutorial background |
| `ts.tutorial1` | Tutorial 1 |
| `ts.tutorial2` | Tutorial 2 |
| `ts.tutorial3` | Tutorial 3 |
| `ts.tutorial4` | Tutorial 4 |
| `ts.tutorial5` | Tutorial 5 |

## Inventory
| Command | Description |
|---|---|
| `inventory.addbooster` | Add a booster pack to inventory |
| `inventory.addcards` | Add number of cards to inventory |
| `inventory.addchest` | Add a chest (Promo/PVE only) to inventory |
| `inventory.addcoin` | Add a named coin to inventory |
| `inventory.addcommonpack` | Add common pack(s) for the given set to inventory |
| `inventory.addconstructedticket` | Add a constructed comp ticket to inventory |
| `inventory.addcosmiconeticket` | Add a day 1 cosmic constructed ticket |
| `inventory.addcosmictwoticket` | Add a day 2 cosmic constructed ticket |
| `inventory.adddeck` | Add new template deck to inventory |
| `inventory.adddecksleeve` | Add a named deck sleeve to inventory |
| `inventory.adddraftticket` | Add a draft comp ticket to inventory |
| `inventory.addequipment` | Add a piece of equipment to inventory |
| `inventory.addfullsetpack` | Add full set pack(s) to inventory |
| `inventory.addgems` | Add a number of each kind of gems to inventory |
| `inventory.addgold` | Add gold |
| `inventory.addmercenary` | Add a mercenary to inventory |
| `inventory.addnthcard` | Add nth matching card to inventory |
| `inventory.addplatinum` | Add platinum |
| `inventory.addprimalpack` | Add a primal pack to inventory |
| `inventory.addproticket` | Add a pro ticket to inventory |
| `inventory.addqualifiertickets` | Add 10 qualifier tickets to inventory |
| `inventory.addsealedticket` | Add a sealed comp ticket to inventory |
| `inventory.addsetboosters` | Add booster pack(s) for the given set |
| `inventory.addvipticket` | Add a VIP comp ticket to inventory |
| `inventory.listpacks` | List all card packs the user has |
| `inventory.merc-add-all` | Add all mercenaries to your account |
| `inventory.openpack` | Open a card pack (by index from listpacks) |
| `inventory.showpacks` | Show pack opening interface |

## Ladder
| Command | Description |
|---|---|
| `ladder.close` | Close ladder |
| `ladder.lose` | Lose ladder match |
| `ladder.reset` | Reset ladder |
| `ladder.win` | Win ladder match |

## Onboarding
| Command | Description |
|---|---|
| `onboarding.complete` | Completes all current quests |
| `onboarding.disable` | Disables the onboarding experience |
| `onboarding.enable` | Enables the onboarding experience |
| `onboarding.reload` | Reloads your current quests |
| `onboarding.reset` | Resets all onboarding progress |

## Profile
| Command | Description |
|---|---|
| `profile.axp-claim-all` | Claim all not-claimed assets for this account's level |
| `profile.axp-claim-reset` | Clear all AXP claims to allow reclaim testing |
| `profile.axp-event` | Generate an AXP event PVPWIN or PVEWIN |
| `profile.axp-move-timer` | Move AXP cap timer +/- minutes |
| `profile.axp-onetime` | Generate an AXP onetime event (TUTORIAL) |
| `profile.axp-onetime-reset` | Clear an AXP onetime event |
| `profile.axp-set` | Set this account's XP to some number |
| `profile.item-cur-award` | Add item type currency (Siege Sacks, Cosmic Coins) |
| `profile.merc-ch-comp` | Complete a challenge for a merc |

## Resources
| Command | Description |
|---|---|
| `resources.forcepurge` | Purges ALL textures (DANGEROUS!) |
| `resources.purge` | Purges unused textures (ref count == 0) |
| `resources.refcounts` | List ref counts of all textures |

## Siege
| Command | Description |
|---|---|
| `landing.togglesiege` | Toggle the siege enabled flag |
| `siege.lock` | Lock the Siege server |
| `siege.unlock` | UnLock the Siege server |

## Tournaments
| Command | Description |
|---|---|
| `tournament.addcardtopool` | Adds a card to your limited pool |
| `tournament.addfuse` | Adds a fuse card to your limited pool |
| `tournament.beginloadtest` | Load test tournament server |
| `tournament.close` | Closes a tournament and eliminates all players |
| `tournament.create` | Creates a new constructed tournament (2/4/8 players) |
| `tournament.createcustom` | Creates a new tournament |
| `tournament.createdaily` | Creates a new daily tournament (0=Constructed, 1=Sealed) |
| `tournament.createpro` | Creates a new pro tournament |
| `tournament.createvip` | Creates a new VIP tournament |
| `tournament.createwaitingroom` | Creates a tournament waiting room |
| `tournament.disqualify` | Disqualifies a player from a tournament |
| `tournament.invalidatecache` | Invalidates tournament cache and reloads templates |
| `tournament.progress` | Progress a moderated tournament |
| `tournament.restartmatch` | Restart a match in a moderated tournament |
| `tournament.setlimitedcooldown` | Sets the limited cooldown duration |
| `tournament.simulation` | Runs a simulated mass tournament |
| `tournament.testresultspopup` | Show tournament results popup with dummy data |
| `tournament.toggledraftspeed` | Toggles between Rapid and Normal draft speeds |
| `tournament.toggletournamentalias` | Toggle tournament message routing |

## Wheel of Fate
| Command | Description |
|---|---|
| `wheeloffate.spin` | Spin the wheel of fate |
| `wof.Set1Fakewin` | Awards all Set 1 items into loot window |
| `wof.Set2Fakewin` | Awards all Set 2 items into loot window |

## Misc
| Command | Description |
|---|---|
| `aa.settimer` | Set your addiction timer to a number of seconds |
| `clear` | Clears the console window |
| `config.apply` | Apply a value to a config field |
| `conversation.test` | Conversation Window tester |
| `exchange.show` | Show exchange |
| `help` | List all available commands (or search) |
| `help.save` | Save help output to a file |
| `log.configure` | Configure logging |
| `leaderboard.show` | Show the leaderboard |
| `replay.load` | Load and play a replay |
