import readmeMd from './README.md?raw'
import afusektMd from './afusekt.md?raw'
import baomiMd from './baomi.md?raw'
import capyplayerMd from './capyplayer.md?raw'
import eplayerxMd from './eplayerx.md?raw'
import filebarMd from './filebar.md?raw'
import forwardMd from './forward.md?raw'
import hamhubMd from './hamhub.md?raw'
import hillsMd from './hills.md?raw'
import hosplayerMd from './hosplayer.md?raw'
import infuseMd from './infuse.md?raw'
import lennaMd from './lenna.md?raw'
import rexMd from './rex.md?raw'
import senplayerMd from './senplayer.md?raw'
import vidhubMd from './vidhub.md?raw'
import xiaohuanMd from './xiaohuan.md?raw'
import yambyMd from './yamby.md?raw'

export interface TutorialDocEntry {
  slug: string
  title: string
  markdown: string
}

export const PLAYER_TUTORIAL_DOCS: Record<string, TutorialDocEntry> = {
  readme: { slug: 'readme', title: '播放器新手总入门与设备选择指南', markdown: readmeMd },
  infuse: { slug: 'infuse', title: 'Infuse 连接与播放教程', markdown: infuseMd },
  vidhub: { slug: 'vidhub', title: 'VidHub 连接与播放教程', markdown: vidhubMd },
  senplayer: { slug: 'senplayer', title: 'SenPlayer 连接与播放教程', markdown: senplayerMd },
  forward: { slug: 'forward', title: 'Forward（新视界）连接与播放教程', markdown: forwardMd },
  rex: { slug: 'rex', title: 'Rex 连接与播放教程', markdown: rexMd },
  capyplayer: { slug: 'capyplayer', title: 'CapyPlayer（卡皮巴拉）连接与播放教程', markdown: capyplayerMd },
  hamhub: { slug: 'hamhub', title: 'HamHub 连接与播放教程', markdown: hamhubMd },
  lenna: { slug: 'lenna', title: 'Lenna 连接与播放教程', markdown: lennaMd },
  eplayerx: { slug: 'eplayerx', title: 'EplayerX 连接与播放教程', markdown: eplayerxMd },
  filebar: { slug: 'filebar', title: 'Filebar（原 Fileball）连接与播放教程', markdown: filebarMd },
  baomi: { slug: 'baomi', title: '网易爆米花连接与播放教程', markdown: baomiMd },
  afusekt: { slug: 'afusekt', title: 'AfuseKt 与 AfuseKtV 连接与播放教程', markdown: afusektMd },
  yamby: { slug: 'yamby', title: 'Yamby 连接与播放教程', markdown: yambyMd },
  hills: { slug: 'hills', title: 'Hills 与 Hills Lite 连接与播放教程', markdown: hillsMd },
  xiaohuan: { slug: 'xiaohuan', title: '小幻影视（Rodel Player）连接与播放教程', markdown: xiaohuanMd },
  hosplayer: { slug: 'hosplayer', title: 'hosplayer 连接与播放教程', markdown: hosplayerMd },
}
