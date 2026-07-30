#ifndef MULTIFILE_H
#define MULTIFILE_H

#include "MultiFile_global.h"
#include "TUDefines.h"

MULTIFILE_EXPORT	bool Init_MultiFile(
    char                *pstrPath,      /*	[in] the IMG Path .tiff or .raw	 */
    int					width,			/*	[in] the IMG Width			*/
    int					height,			/*	[in] the IMG Height			*/
    int					channels,		/*	[in] the IMG Channel 1\3	*/
    int					depth,			/*	[in] the IMG Depth Data 8\16	*/
    int                 stackNumber,    /*  [in] the IMG StackNumber    */
    int                 blocks = 1,
    int                 index = 0
    );      /* Create IMG File */

MULTIFILE_EXPORT	bool Open_MultiFile(char *pstrPath = nullptr, int index = 0);  /*	Open File	*/

MULTIFILE_EXPORT	bool Append_FileBuffer(
    const unsigned char	*pBuffer,	    /*	[in] the IMG Buffer		*/
    int                 bufferSize,     /*  [in] the IMG Data Size  */
    int                 index = 0       /*  [in] the IMG Data Index */
    );

MULTIFILE_EXPORT	bool Close_MultiFile(int index = 0);                           /*	Close File	*/

MULTIFILE_EXPORT	bool Uninit_MultiFile(int index = 0);                           /*	Uninit File	*/

#endif // MULTIFILE_H
